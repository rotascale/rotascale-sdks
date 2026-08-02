"""The MCP server-side guard — the end of the wire the proxy cannot cover.

subhadipmitra@: `proxy.py` is a real enforcement point with one gap the epic
states plainly: it is bypassable by not using that tool. An agent talking to the
MCP server directly, or a second agent nobody routed through the proxy, never
meets it.

This guard lives in the server, so there is nothing to route around. The tests
that matter are the ones where a token exists and still must not be enough: a
token for a different tool on the same server, an argument set the rule cannot
read, and a protected tool nobody wrote a rule for.
"""

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from rotascale.capability import Refused

from rotascale_mcp.guard import (
    ARGUMENT_KEY,
    META_KEY,
    CapabilityGuard,
    MisconfiguredGuard,
    refusal_result,
)

AUDIENCE = "mcp://contracts.acme.internal"
KID = "ed25519:TESTKEY"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture
def signing_key():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def jwks(signing_key):
    raw = signing_key.public_key().public_bytes_raw()
    return {"keys": [{"kty": "OKP", "crv": "Ed25519", "x": _b64(raw),
                      "alg": "EdDSA", "use": "sig", "kid": KID}]}


def _token(signing_key, *, aud=AUDIENCE, tool="settle_payment", amount=25_000,
           jti="cap_01TEST", exp_offset=60):
    claims = {
        "aud": aud, "jti": jti,
        "iat": int(time.time()), "exp": int(time.time()) + exp_offset,
        "sub": "agt_01TEST",
        "act": {"tool": tool, "amount_minor": amount, "currency": "SGD"},
        "rot": {"grant_id": "grt_01TEST", "ledger_id": "led_01TEST",
                "attested_by": "sarah.bennett@acme.example",
                "signed_by_platform": False, "enforcement_tier": "observed"},
    }
    header = {"alg": "EdDSA", "typ": "JWT", "kid": KID}
    part = (f"{_b64(json.dumps(header, sort_keys=True, separators=(',', ':')).encode())}."
            f"{_b64(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode())}")
    return f"{part}.{_b64(signing_key.sign(part.encode()))}"


def _guard(jwks, **kwargs):
    kwargs.setdefault(
        "amounts", {"settle_payment": lambda args: args["amount_minor"]})
    guard = CapabilityGuard(audience=AUDIENCE, **kwargs)
    guard.load_keys(jwks)
    return guard


class TestNoTokenNoCall:
    def test_a_protected_tool_without_a_token_is_refused(self, jwks):
        guard = _guard(jwks)
        with pytest.raises(Refused, match="no capability token"):
            guard.check("settle_payment", {"amount_minor": 100})

    def test_an_unprotected_tool_passes_through(self, jwks):
        """`protect` names the guarded tools; everything else is untouched."""
        guard = _guard(jwks, protect=["settle_payment"])
        assert guard.check("read_status", {"x": 1}) == {"x": 1}

    def test_everything_is_protected_by_default(self, jwks):
        """A server that lists its protected tools will forget one."""
        guard = _guard(jwks)
        with pytest.raises(Refused):
            guard.check("some_tool_nobody_thought_about", {})


class TestATokenIsNotEnoughOnItsOwn:
    def test_a_token_for_another_server_is_refused(self, jwks, signing_key):
        guard = _guard(jwks)
        token = _token(signing_key, aud="mcp://payroll.acme.internal")
        with pytest.raises(Refused):
            guard.check("settle_payment",
                        {"amount_minor": 100, ARGUMENT_KEY: token})

    def test_a_token_for_a_different_tool_on_this_server_is_refused(
            self, jwks, signing_key):
        """`aud` names the server, not the tool.

        subhadipmitra@: Without this check a token minted for a cheap read
        would authorise an expensive write on the same server — the audience
        matches, the signature is valid, and the action is entirely different.
        """
        guard = _guard(jwks, amounts={
            "settle_payment": lambda a: a["amount_minor"],
            "read_balance": lambda a: 0,
        })
        token = _token(signing_key, tool="read_balance")
        with pytest.raises(Refused, match="authorises 'read_balance'"):
            guard.check("settle_payment",
                        {"amount_minor": 100, ARGUMENT_KEY: token})

    def test_asking_for_more_than_the_token_covers_is_refused(
            self, jwks, signing_key):
        """The comparison a signature cannot make."""
        guard = _guard(jwks)
        token = _token(signing_key, amount=1_200)
        with pytest.raises(Refused, match="authorised for 1200, asked for 12000"):
            guard.check("settle_payment",
                        {"amount_minor": 12_000, ARGUMENT_KEY: token})

    def test_a_protected_tool_with_no_rule_is_refused_not_allowed(
            self, jwks, signing_key):
        """The omission that would quietly make the guard decorative.

        subhadipmitra@: A protected tool nobody wrote a comparison for is one
        nobody decided about. Allowing it would mean the token's existence
        silently became the whole authorisation — which is a real position, and
        one somebody has to take deliberately by naming the tool in
        `quantityless`.
        """
        guard = _guard(jwks, amounts={})
        token = _token(signing_key)
        with pytest.raises(Refused, match="no way to compare"):
            guard.check("settle_payment", {ARGUMENT_KEY: token})

    def test_a_quantityless_tool_is_allowed_once_it_is_declared(
            self, jwks, signing_key):
        guard = _guard(jwks, amounts={}, quantityless=["publish_report"])
        token = _token(signing_key, tool="publish_report")
        assert guard.check("publish_report", {ARGUMENT_KEY: token}) == {}

    def test_arguments_the_rule_cannot_read_are_unbounded_not_zero(
            self, jwks, signing_key):
        """Otherwise a malformed call is the way around the check."""
        guard = _guard(jwks, amounts={
            "settle_payment": lambda a: a["missing_key"]})
        token = _token(signing_key)
        with pytest.raises(Refused, match="authorised for 25000"):
            guard.check("settle_payment", {"wrong": 1, ARGUMENT_KEY: token})


class TestTheToolNeverSeesTheToken:
    def test_the_token_is_stripped_from_arguments(self, jwks, signing_key):
        """A tool whose signature grew a parameter because of us is one we broke."""
        guard = _guard(jwks)
        token = _token(signing_key)
        cleaned = guard.check(
            "settle_payment", {"amount_minor": 100, ARGUMENT_KEY: token})

        assert cleaned == {"amount_minor": 100}
        assert ARGUMENT_KEY not in cleaned

    def test_meta_is_accepted_and_preferred(self, jwks, signing_key):
        """`_meta` is the protocol's own slot for out-of-band data."""
        guard = _guard(jwks)
        token = _token(signing_key)
        cleaned = guard.check(
            "settle_payment", {"amount_minor": 100},
            meta={META_KEY: token})

        assert cleaned == {"amount_minor": 100}


class TestReplay:
    def test_the_same_token_cannot_act_twice(self, jwks, signing_key):
        """A bearer credential presented twice must not act twice.

        subhadipmitra@: The HTTP guard can replay a recorded response, so an
        honest retry is answered rather than refused. There is no recorded MCP
        result to hand back here, so this refuses instead of pretending — which
        is the honest behaviour, and the reason two-phase settlement exists for
        anything consequential.
        """
        guard = _guard(jwks)
        token = _token(signing_key)
        guard.check("settle_payment", {"amount_minor": 100, ARGUMENT_KEY: token})

        with pytest.raises(Refused):
            guard.check("settle_payment",
                        {"amount_minor": 100, ARGUMENT_KEY: token})


class TestConfiguration:
    def test_a_tool_cannot_be_both_measured_and_quantityless(self, jwks):
        """One of the two is wrong, and guessing which would decide how much
        authority the tool needs."""
        with pytest.raises(MisconfiguredGuard, match="both"):
            CapabilityGuard(
                audience=AUDIENCE,
                amounts={"settle_payment": lambda a: 0},
                quantityless=["settle_payment"])


class TestTheRefusalAnAgentSees:
    def test_it_is_a_tool_result_not_a_protocol_error(self):
        """The same choice `proxy.py` made, for the same reason.

        A JSON-RPC error is usually swallowed by the host and shown to the model
        as "the tool failed", which invites a retry. A result puts the sentence
        in front of the model, where it can be read and obeyed.
        """
        reply = refusal_result(7, "authorised for 1200, asked for 12000")

        assert reply["id"] == 7
        assert "error" not in reply
        assert reply["result"]["isError"] is True
        assert reply["result"]["_rotascale"]["enforced_at"] == "resource"
        assert "1200" in reply["result"]["content"][0]["text"]
