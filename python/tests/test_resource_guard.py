"""The reference validator, driven as a real ASGI application (`#96` step 3).

subhadipmitra@: `rotascale.capability.verify` is the primitive and a customer
can wire it up in an afternoon. An afternoon per resource, times every resource
that matters, is the reason nobody does. This is the same guarantee in one line
of configuration, and these tests exist because a guard that is easy to install
is also easy to install WRONG.

The cases that matter most are the ones where the guard would still look like it
was working: a token that verifies but does not cover the request, a body the
extractor cannot read, and a validator constructed without the comparison at all.
"""

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from rotascale.capability import public_keys_from_jwks
from rotascale.resource import MisconfiguredValidator, RequireCapability

AUDIENCE = "https://payments.acme.internal"
KID = "ed25519:TESTKEY"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


@pytest.fixture
def signing_key():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def keys(signing_key):
    raw = signing_key.public_key().public_bytes_raw()
    return public_keys_from_jwks({"keys": [{
        "kty": "OKP", "crv": "Ed25519", "x": _b64(raw),
        "alg": "EdDSA", "use": "sig", "kid": KID}]})


def _token(signing_key, *, aud=AUDIENCE, amount=25_000, jti="cap_01TEST",
           exp_offset=60):
    claims = {
        "aud": aud, "jti": jti,
        "iat": int(time.time()), "exp": int(time.time()) + exp_offset,
        "sub": "agt_01TEST",
        "act": {"tool": "issue_refund", "amount_minor": amount,
                "currency": "EUR", "resource_ref": "TICKET-88123"},
        "rot": {"grant_id": "grt_01TEST", "ledger_id": "led_01TEST",
                "attested_by": "sarah.bennett@acme.example",
                "signed_by_platform": False, "enforcement_tier": "observed"},
    }
    header = {"alg": "EdDSA", "typ": "JWT", "kid": KID}
    part = (f"{_b64(json.dumps(header, sort_keys=True, separators=(',', ':')).encode())}."
            f"{_b64(json.dumps(claims, sort_keys=True, separators=(',', ':')).encode())}")
    return f"{part}.{_b64(signing_key.sign(part.encode()))}"


# --- a real resource behind the guard --------------------------------------


class Payments:
    """The service being protected. Counts how often it actually ran.

    subhadipmitra@: The counter is the point of several tests below. A guard
    that refuses correctly and still lets the application execute has not
    protected anything, and a status code alone would not catch that.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, scope, receive, send):
        message = await receive()
        body = json.loads(message.get("body") or b"{}")
        self.calls += 1
        payload = json.dumps(
            {"refund_id": f"rf_{self.calls}",
             "amount_minor": body.get("amount_minor")}).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": payload})


async def post(guard, path="/refund", token=None, body=None):
    """Drive the middleware over raw ASGI, as a server would."""
    raw = json.dumps(body if body is not None else {}).encode()
    headers = [(b"content-type", b"application/json")]
    if token:
        headers.append((b"authorization", f"Bearer {token}".encode()))

    sent = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": raw, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await guard({"type": "http", "path": path, "method": "POST",
                 "headers": headers, "client": ("203.0.113.9", 51234)},
                receive, send)

    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent
                       if m["type"] == "http.response.body")
    header_map = {
        k.decode().lower(): v.decode()
        for m in sent if m["type"] == "http.response.start"
        for k, v in m.get("headers", [])
    }
    return status, json.loads(payload or b"{}"), header_map


def _guard(app, keys, **kwargs):
    kwargs.setdefault("amount_minor", lambda body: body.get("amount_minor", 0))
    kwargs.setdefault("report_replays", False)     # no network in tests
    guard = RequireCapability(
        app, audience=AUDIENCE, protect=["/refund"], **kwargs)
    guard._keys = keys                              # already fetched
    return guard


# --- the misconfiguration that would make it decorative ---------------------


def test_a_guard_without_the_action_comparison_refuses_to_be_built():
    """The easy mistake, caught where it can still be fixed.

    subhadipmitra@: A validator that checks the signature and not the action
    returns 200 for every legitimate request and 403 for an unsigned one — it
    looks exactly like a working guard, and it would wave through an agent
    asking ten times what it was authorised for.

    Refusing at construction is the only moment this is visible before it
    matters.
    """
    with pytest.raises(MisconfiguredValidator) as caught:
        RequireCapability(Payments(), audience=AUDIENCE, protect=["/refund"])

    assert "amount_minor" in str(caught.value)
    assert "signature_only" in str(caught.value), "the error must name the escape"


def test_signature_only_is_available_but_must_be_asked_for():
    """A resource with no quantity is legitimate; it just has to say so."""
    guard = RequireCapability(
        Payments(), audience=AUDIENCE, protect=["/x"], signature_only=True)
    assert guard.signature_only is True


# --- the refusals -----------------------------------------------------------


async def test_no_token_is_refused_and_the_service_never_runs(keys, signing_key):
    app = Payments()
    status, body, _ = await post(_guard(app, keys))

    assert status == 403
    assert "no capability token" in body["refused"]
    assert app.calls == 0, "the guard must be ON the path, not beside it"


async def test_a_token_for_another_resource_is_refused(keys, signing_key):
    """What stops a refunds credential being replayed against payroll."""
    app = Payments()
    token = _token(signing_key, aud="https://payroll.acme.internal")
    status, _, _ = await post(_guard(app, keys), token=token,
                              body={"amount_minor": 100})

    assert status == 403
    assert app.calls == 0


async def test_a_genuine_token_asking_for_more_than_it_covers_is_refused(
        keys, signing_key):
    """The case a signature alone cannot catch, and the reason this exists.

    subhadipmitra@: The credential is real, unexpired, correctly signed and
    minted for this exact resource. It simply does not cover what is being
    asked. A validator that stops at the signature has verified nothing about
    the action in front of it.
    """
    app = Payments()
    token = _token(signing_key, amount=1_200)
    status, body, _ = await post(_guard(app, keys), token=token,
                                 body={"amount_minor": 12_000})

    assert status == 403
    assert "authorised for 1200, asked for 12000" in body["refused"]
    assert app.calls == 0


async def test_an_unreadable_body_is_treated_as_unbounded(keys, signing_key):
    """A value nothing can determine must exceed every ceiling.

    subhadipmitra@: If the extractor failing returned 0, an unparseable request
    would compare "nothing was asked for" against the token and pass — making a
    malformed body the way around the whole check.
    """
    app = Payments()

    def explodes(_body):
        raise KeyError("amount_minor")

    guard = _guard(app, keys, amount_minor=explodes)
    status, _, _ = await post(guard, token=_token(signing_key),
                              body={"whatever": 1})

    assert status == 403
    assert app.calls == 0


async def test_the_service_cannot_check_reports_503_not_403(signing_key):
    """"We could not check" and "you are not authorised" need opposite responses.

    Answering 403 would send somebody debugging the agent when the fault is in
    the validator's own reach.
    """
    app = Payments()
    guard = RequireCapability(
        app, audience=AUDIENCE, protect=["/refund"],
        amount_minor=lambda b: b.get("amount_minor", 0),
        jwks_url="http://127.0.0.1:1/.well-known/jwks.json",
        report_replays=False)

    status, body, _ = await post(guard, token=_token(signing_key),
                                 body={"amount_minor": 100})

    assert status == 503
    assert "cannot verify" in body["refused"]
    assert app.calls == 0


# --- what must still work ---------------------------------------------------


async def test_an_authorised_request_reaches_the_service_with_its_body(
        keys, signing_key):
    """The guard reads the body to compare the action; the app still needs it."""
    app = Payments()
    status, body, _ = await post(_guard(app, keys), token=_token(signing_key),
                                 body={"amount_minor": 25_000})

    assert status == 200
    assert body == {"refund_id": "rf_1", "amount_minor": 25_000}
    assert app.calls == 1


async def test_an_unprotected_path_is_untouched(keys, signing_key):
    """A health endpoint demanding a capability takes the service out of its
    own load balancer."""
    app = Payments()
    status, _, _ = await post(_guard(app, keys), path="/healthz")

    assert status == 200
    assert app.calls == 1


async def test_an_honest_retry_gets_the_same_answer_not_a_refusal(
        keys, signing_key):
    """The case that makes a naive seen-set worse than none.

    subhadipmitra@: The action succeeded, the response was lost, the client
    retries with the same token. Refusing turns one lost packet into a failed
    payment, and the customer's remedy is to stop using us. The service must
    also not run twice.
    """
    app = Payments()
    guard = _guard(app, keys)
    token = _token(signing_key)

    first_status, first_body, _ = await post(
        guard, token=token, body={"amount_minor": 25_000})
    again_status, again_body, headers = await post(
        guard, token=token, body={"amount_minor": 25_000})

    assert first_status == again_status == 200
    assert first_body == again_body, "the same answer, not a second refund"
    assert headers.get("x-rotascale-replayed") == "true"
    assert app.calls == 1, "the money moved exactly once"
