"""The validator a customer drops in front of a resource.

subhadipmitra@: These tests deliberately mint tokens WITHOUT importing anything
from the API. A resource has no Rotascale code in it, so if the fixtures here
needed the server the module would not be proving what it claims.
"""

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rotascale.capability import (
    Claim,
    Refused,
    SeenTokens,
    public_keys_from_jwks,
    verify,
)

AUDIENCE = "https://payments.acme.internal"
KID = "ed25519:test"


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


def _token(signing_key, *, aud=AUDIENCE, kid=KID, exp_offset=60, **overrides):
    claims = {
        "aud": aud, "jti": "cap_01TEST",
        "iat": int(time.time()), "exp": int(time.time()) + exp_offset,
        "sub": "agt_01TEST",
        "act": {"tool": "issue_refund", "amount_minor": 25_000,
                "currency": "EUR", "resource_ref": "TICKET-88123"},
        "rot": {"grant_id": "grt_01TEST", "ledger_id": "led_01TEST",
                "attested_by": "sarah.bennett@acme.example",
                "signed_by_platform": False, "enforcement_tier": "observed"},
    }
    claims.update(overrides)
    header = {"alg": "EdDSA", "typ": "JWT", "kid": kid}
    part = (f"{_b64(json.dumps(header, sort_keys=True, separators=(',', ':')).encode())}."
            f"{_b64(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode())}")
    return f"{part}.{_b64(signing_key.sign(part.encode()))}"


def test_a_valid_token_yields_the_action_it_authorises(signing_key, jwks):
    claim = verify(_token(signing_key), audience=AUDIENCE, jwks=jwks)

    assert isinstance(claim, Claim)
    assert claim.tool == "issue_refund"
    assert claim.amount_minor == 25_000
    assert claim.resource_ref == "TICKET-88123"
    assert claim.attested_by == "sarah.bennett@acme.example"
    # A resource protecting something serious may reasonably require that a
    # PERSON signed, not that our deployment key recorded they did.
    assert claim.signed_by_platform is False


def test_a_token_for_another_resource_is_refused(signing_key, jwks):
    """The check a lazy validator skips. Without it you have verified that SOME
    authority existed, not that THIS one did."""
    with pytest.raises(Refused):
        verify(_token(signing_key, aud="https://payroll.acme.internal"),
               audience=AUDIENCE, jwks=jwks)


def test_an_expired_token_is_refused(signing_key, jwks):
    with pytest.raises(Refused):
        verify(_token(signing_key, exp_offset=-120), audience=AUDIENCE, jwks=jwks)


def test_clock_skew_is_tolerated_within_the_stated_bound(signing_key, jwks):
    """Too tight and a resource with a drifting clock refuses everything."""
    claim = verify(_token(signing_key, exp_offset=-5),
                   audience=AUDIENCE, jwks=jwks, leeway_seconds=30)
    assert claim.tool == "issue_refund"


def test_a_token_signed_by_another_key_is_refused(jwks):
    """The whole point of a signature."""
    with pytest.raises(Refused):
        verify(_token(Ed25519PrivateKey.generate()), audience=AUDIENCE, jwks=jwks)


def test_an_unknown_kid_is_refused_rather_than_tried_against_every_key(
    signing_key, jwks
):
    """subhadipmitra@: During a rotation both keys are published. Silently
    accepting a token whose `kid` names neither would mean the rotation was not
    enforcing anything."""
    with pytest.raises(Refused, match="unknown signing key"):
        verify(_token(signing_key, kid="ed25519:retired"),
               audience=AUDIENCE, jwks=jwks)


def test_a_tampered_amount_is_refused(signing_key, jwks):
    token = _token(signing_key)
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    claims["act"]["amount_minor"] = 10_000_000
    forged = _b64(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())

    with pytest.raises(Refused):
        verify(f"{header}.{forged}.{signature}", audience=AUDIENCE, jwks=jwks)


def test_a_token_without_an_expiry_is_refused(signing_key, jwks):
    """A capability token with no lifetime is a bearer credential forever."""
    token = _token(signing_key)
    header, payload, _ = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    del claims["exp"]
    part = f"{header}.{_b64(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode())}"
    with pytest.raises(Refused):
        verify(f"{part}.{_b64(signing_key.sign(part.encode()))}",
               audience=AUDIENCE, jwks=jwks)


def test_an_empty_jwks_is_refused_rather_than_treated_as_no_checking(signing_key):
    with pytest.raises(Refused):
        verify(_token(signing_key), audience=AUDIENCE, jwks={"keys": []})


def test_jti_is_exposed_so_a_resource_can_refuse_replay(signing_key, jwks):
    """Replay defence belongs to the resource: the window it must cover is only
    the token's lifetime, which is seconds."""
    claim = verify(_token(signing_key), audience=AUDIENCE, jwks=jwks)
    assert claim.jti == "cap_01TEST"
    assert claim.expires_at > time.time()


def test_keys_can_be_supplied_directly_so_the_fetch_stays_the_callers(
    signing_key, jwks
):
    """A validator that reaches for the network on its own fails in ways the
    resource cannot control."""
    keys = public_keys_from_jwks(jwks)
    assert verify(_token(signing_key), audience=AUDIENCE, keys=keys).tool == "issue_refund"


# --- replay defence ---------------------------------------------------------


def _claim(jti: str = "cap_01A", exp_offset: int = 60) -> Claim:
    return Claim(tool="issue_refund", amount_minor=1, currency="EUR",
                 resource_ref="TICKET-1", jti=jti,
                 expires_at=int(time.time()) + exp_offset,
                 grant_id=None, ledger_id=None, attested_by=None,
                 signed_by_platform=True, enforcement_tier="observed", raw={})


def test_the_first_presentation_proceeds():
    seen = SeenTokens()
    assert seen.remember(_claim()) is None


def test_an_honest_retry_gets_THE_SAME_ANSWER_not_a_refusal():
    """subhadipmitra@: The case that makes a naive seen-set worse than none.

    The action succeeded, the response was lost, the client retries with the
    same token. Refusing turns one lost packet into a failed payment, and the
    customer's remedy is to stop using us.
    """
    seen, claim = SeenTokens(), _claim()

    assert seen.remember(claim) is None
    seen.record(claim, {"refund_id": "rf_991"})

    # Same token again — the recorded outcome, not an error.
    assert seen.remember(claim) == {"refund_id": "rf_991"}


def test_a_concurrent_replay_is_refused_rather_than_allowed_to_act():
    """subhadipmitra@: The bug a weaker version of this test hid.

    Presented twice before the first presentation finishes, a naive seen-set
    returns None — indistinguishable from "first time, proceed" — and the
    second caller acts. There is no recorded answer to hand back, so the only
    honest options are refuse or act twice, and acting twice is the thing this
    class exists to prevent.
    """
    seen, claim = SeenTokens(), _claim()
    assert seen.remember(claim) is None          # first, in flight

    with pytest.raises(Refused, match="already in flight"):
        seen.remember(claim)

    assert len(seen) == 1, "the token was counted twice"


def test_an_action_that_legitimately_returned_none_is_not_mistaken_for_in_flight():
    """A resource whose action returns nothing must still get idempotency."""
    seen, claim = SeenTokens(), _claim()
    assert seen.remember(claim) is None
    seen.record(claim, None)                     # the action returned None

    assert seen.remember(claim) is None          # the recorded answer, not a refusal


def test_a_different_token_is_not_confused_with_a_replay():
    seen = SeenTokens()
    assert seen.remember(_claim("cap_01A")) is None
    assert seen.remember(_claim("cap_01B")) is None
    assert len(seen) == 2


def test_the_set_bounds_itself_by_token_lifetime():
    """subhadipmitra@: No eviction policy anybody has to tune. An entry older
    than any possible unexpired token cannot be replayed anyway."""
    seen = SeenTokens()
    seen.remember(_claim("cap_old", exp_offset=-10))
    assert len(seen) == 1

    # A later presentation sweeps anything that can no longer be valid.
    seen.remember(_claim("cap_new"), now=int(time.time()) + 1)
    assert len(seen) == 1
    assert "cap_old" not in seen._seen
