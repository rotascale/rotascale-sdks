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

__all__ = ["Claim", "Refused", "public_keys_from_jwks", "verify"]


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
