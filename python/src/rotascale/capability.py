"""Verify a capability token at the resource. No Rotascale call, no SDK client.

subhadipmitra@: This is the enforcement point. Everything else in this SDK is
*advice* — on the instrumented path the code that asks and the code that acts
are the same code, so an agent can simply not ask. Here the RESOURCE checks, and
an agent that cannot produce a valid token cannot act, wherever it runs.

    from rotascale.capability import verify, Refused

    KEYS = fetch_jwks("https://rotascale.acme.internal/.well-known/jwks.json")

    try:
        claim = verify(token, audience="https://payments.acme.internal", jwks=KEYS)
    except Refused as exc:
        return 403, str(exc)

    if claim.amount_minor < requested_amount:
        return 403, "authorised for less than requested"

**Offline.** The JWKS is fetched once and cached; verification afterwards makes
no network call. Our availability is therefore not the customer's availability,
and that is the property that makes this adoptable at all.

**Deliberately small.** If integrating takes a week nobody integrates, and the
enforcement point stays hypothetical. The only hard dependency is a JWT library.

## What this module refuses to make optional

A validator that checks the signature and stops has verified that *some*
authority existed, not that *this* one did. So `verify` checks the audience and
requires the caller to compare the action — `Claim` deliberately exposes
`amount_minor`, `tool` and `resource_ref` rather than hiding them, because the
comparison against what was actually requested is the customer's to make and
cannot be done here.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

__all__ = ["Claim", "Refused", "SeenTokens", "public_keys_from_jwks", "verify"]


class Refused(Exception):
    """The token does not authorise this. Deny the action.

    subhadipmitra@: One exception type for every reason, on purpose. A resource
    should not be branching on *why* a credential failed — that is how a
    handler ends up with an `except ExpiredSignature: pass` somebody added
    during an incident and nobody removed.
    """


@dataclass(frozen=True)
class Claim:
    """What the token says was authorised."""

    #: The tool named in the authorisation.
    tool: str | None
    #: Money, in MINOR units, matching how the grant was written.
    amount_minor: int
    currency: str | None
    #: Whatever the agent was acting on — a ticket, an invoice, an account.
    resource_ref: str | None

    #: Unique per token. Keep a short-lived set of these to refuse replay; the
    #: window you must cover is only the token's lifetime, which is seconds.
    jti: str
    expires_at: int

    #: Provenance, for logging and for a resource that wants to be stricter.
    grant_id: str | None
    ledger_id: str | None
    #: The named human who attested the bounds, if one did.
    attested_by: str | None
    #: False when a person signed with their own key; True when only the
    #: deployment key recorded that they attested. A resource protecting
    #: something serious may reasonably require False.
    signed_by_platform: bool
    #: How well the tool name was known upstream: `asserted` (the agent said
    #: so), `observed` (read off the wire), or `resource` (verified here).
    enforcement_tier: str | None

    raw: dict[str, Any]


def public_keys_from_jwks(jwks: dict[str, Any]) -> dict[str, Any]:
    """Turn a JWK set into `{kid: key}` for the JWT library.

    Kept separate so the fetch — and therefore the caching, the retries and the
    HTTP client — stays the caller's. A validator that reaches for the network
    on its own is one that fails in ways the resource cannot control.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    keys: dict[str, Any] = {}
    for jwk in jwks.get("keys", []):
        if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
            continue
        padded = jwk["x"] + "=" * (-len(jwk["x"]) % 4)
        keys[jwk.get("kid") or ""] = Ed25519PublicKey.from_public_bytes(
            base64.urlsafe_b64decode(padded)
        )
    if not keys:
        raise Refused("the JWK set contains no Ed25519 signing key")
    return keys


def verify(
    token: str,
    *,
    audience: str,
    jwks: dict[str, Any] | None = None,
    keys: dict[str, Any] | None = None,
    leeway_seconds: int = 30,
) -> Claim:
    """Check a capability token, or raise `Refused`.

    `audience` is THIS resource's own identifier and is not optional. A token
    minted for the refunds service must not be accepted by payroll, and the
    audience is the only thing that stops it.

    `leeway_seconds` bounds clock skew. Too generous and the short lifetime
    stops meaning anything; too tight and a resource whose clock drifts refuses
    everything. Thirty seconds against a sixty-second token is the compromise —
    state it rather than leaving each integration to guess.
    """
    try:
        import jwt
    except ImportError as exc:                                  # pragma: no cover
        raise Refused(
            "PyJWT is required to verify capability tokens: pip install pyjwt"
        ) from exc

    if keys is None:
        if jwks is None:
            raise Refused("no verification key supplied")
        keys = public_keys_from_jwks(jwks)

    try:
        header = jwt.get_unverified_header(token)
    except Exception as exc:
        raise Refused(f"not a usable token: {exc}") from exc

    key = keys.get(header.get("kid") or "")
    if key is None:
        # subhadipmitra@: Refuse rather than try every key. During a rotation
        # both are published, and silently accepting a token whose `kid` names
        # neither would mean the rotation was not actually enforcing anything.
        raise Refused(f"unknown signing key {header.get('kid')!r}")

    try:
        claims = jwt.decode(
            token, key, algorithms=["EdDSA"], audience=audience,
            leeway=leeway_seconds,
            options={"require": ["exp", "aud", "jti"]},
        )
    except Exception as exc:
        raise Refused(f"token rejected: {exc}") from exc

    act = claims.get("act") or {}
    rot = claims.get("rot") or {}
    return Claim(
        tool=act.get("tool"),
        amount_minor=int(act.get("amount_minor") or 0),
        currency=act.get("currency"),
        resource_ref=act.get("resource_ref"),
        jti=claims["jti"],
        expires_at=int(claims["exp"]),
        grant_id=rot.get("grant_id"),
        ledger_id=rot.get("ledger_id"),
        attested_by=rot.get("attested_by"),
        signed_by_platform=bool(rot.get("signed_by_platform", True)),
        enforcement_tier=rot.get("enforcement_tier"),
        raw=claims,
    )


class SeenTokens:
    """Refuse a replayed capability token — without breaking honest retries.

    subhadipmitra@: A capability token is a bearer credential. Anyone holding
    one can present it, so a resource must refuse the second presentation.

    **The case that makes a naive seen-set worse than none** is the honest
    retry: the action succeeded, the response was lost in the network, and the
    client retries with the same token. Refusing that turns one lost packet
    into a failed payment, and the customer's remedy is to stop using us. So
    this remembers what the action RETURNED and replays that answer, which is
    idempotency rather than refusal.

    A replay from a *different* caller is still indistinguishable from a
    retry here — that is the honest limit of a seen-set, and it is why
    two-phase settlement (`#108`) is the real answer for consequential
    actions. This is the floor, not the ceiling.

    Bounded by the token lifetime, not by a count. Entries older than any
    possible unexpired token cannot be replayed anyway, so the set stays small
    on its own and needs no eviction policy anybody has to tune.

        seen = SeenTokens()

        claim = verify(token, audience=AUD, jwks=KEYS)
        cached = seen.remember(claim)
        if cached is not None:
            return cached                    # honest retry: the same answer
        result = do_the_refund(claim)
        seen.record(claim, result)
        return result

    Single process only. A resource behind several replicas needs a shared
    store — Redis with the same TTL — and this is deliberately not that, so
    nobody mistakes an in-memory set for a distributed guarantee.
    """

    #: Distinguishes "recorded, no result yet" from "recorded, returned None".
    #: Without it a token presented twice CONCURRENTLY reads as a first
    #: presentation, and the second caller acts — the exact thing this refuses.
    _IN_FLIGHT = object()

    def __init__(self) -> None:
        self._seen: dict[str, tuple[int, Any]] = {}

    def remember(self, claim: Claim, *, now: int | None = None) -> Any | None:
        """Record this token, or return the result the first presentation gave.

        Returns None the first time — proceed. Returns the recorded result on a
        replay, which the caller should return instead of acting again.
        """
        import time

        current = now if now is not None else int(time.time())
        self._evict(current)

        if claim.jti in self._seen:
            result = self._seen[claim.jti][1]
            if result is self._IN_FLIGHT:
                # Presented again before the first presentation finished. That
                # is either a genuine concurrent replay or a client retrying
                # faster than the action completes, and neither may act twice.
                # There is no recorded answer to hand back, so this refuses.
                raise Refused(
                    f"capability {claim.jti} is already in flight; refusing to "
                    f"act on it a second time")
            return result
        # Held until the token could no longer be valid to anyone.
        self._seen[claim.jti] = (claim.expires_at, self._IN_FLIGHT)
        return None

    def record(self, claim: Claim, result: Any) -> None:
        """Attach the outcome, so a retry gets the answer rather than a refusal."""
        if claim.jti in self._seen:
            self._seen[claim.jti] = (claim.expires_at, result)

    def _evict(self, now: int) -> None:
        expired = [jti for jti, (exp, _) in self._seen.items() if exp < now]
        for jti in expired:
            del self._seen[jti]

    def __len__(self) -> int:
        return len(self._seen)
