"""Make your service refuse an agent that was not authorised.

subhadipmitra@: This is the resource side of the wire, and it is the half that
turns recording into enforcement.

An in-process SDK asking `t.authorize()` is beside the action: the code that
asks permission and the code that acts are the same code, and it can skip the
question. A resource that refuses without a valid capability token is *on* the
causal path, and the agent gets no vote.

The bar the epic set for this was "something a customer can implement in their
own service in an afternoon". That is still true — `rotascale.capability.verify`
is forty lines of use — but an afternoon per resource, times every resource that
matters, is the reason nobody does it. This is the same thing in one line:

    app.add_middleware(
        RequireCapability,
        audience="https://payments.acme.internal",
        protect=["/refund"],
        amount_minor=lambda body: body["amount_minor"],
    )

## What it holds of ours: a public key

Nothing else. No API key, no session, no callback. The JWK set is fetched once
and verification is offline afterwards, so Rotascale being down does not stop
your service working — which is the property that makes putting this on a
payment path defensible at all.

The exception is revocation: a grant revoked while we are unreachable cannot
reach a cached validator. The exposure window is the token lifetime, which is
why it is seconds.

## The check a lazy validator skips

Verifying the signature proves *some* authority existed. Comparing the token's
`act` against what was actually asked for proves *this* one did. A validator
that does the first and not the second has verified nothing about the action in
front of it — the credential is genuine and the request exceeds it, which is
precisely the case a signature cannot catch.

A middleware cannot know where "the amount" lives in your request body, so you
supply that. And because omitting it is the easy mistake that quietly makes the
whole thing decorative, omitting it is **refused at construction** rather than
defaulted: you must pass `amount_minor=`, or pass `signature_only=True` and mean
it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from rotascale.capability import (
    Refused,
    SeenTokens,
    public_keys_from_jwks,
    report_incident,
    verify,
)

logger = logging.getLogger("rotascale.resource")

__all__ = ["RequireCapability"]

DEFAULT_JWKS_URL = "https://api.rotascale.com/.well-known/jwks.json"


class MisconfiguredValidator(ValueError):
    """The guard was set up in a way that would not actually guard anything."""


class RequireCapability:
    """ASGI middleware: refuse a protected route without a valid capability.

    Works with anything ASGI — FastAPI, Starlette, Django's async stack,
    Quart — because it speaks the protocol rather than any framework's.

    Parameters
    ----------
    audience:
        This service's own identifier, as the grant names it. A token minted
        for anything else is refused, which is what stops a refunds credential
        being replayed against payroll.
    protect:
        Path prefixes that require a token. Everything else passes through
        untouched — a health endpoint that demanded a capability would take the
        service out of its own load balancer.
    amount_minor:
        Given the decoded request body, return what is ACTUALLY being asked
        for, in minor units. Compared against the token's `act`.
    signature_only:
        Explicitly accept that the action is not compared. Reserved for
        resources where the token's existence is the whole authorisation and
        there is no quantity — and it says so in the logs on every request,
        because it is the weaker mode.
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        audience: str,
        protect: Iterable[str],
        amount_minor: Callable[[dict[str, Any]], int] | None = None,
        jwks_url: str = DEFAULT_JWKS_URL,
        signature_only: bool = False,
        report_replays: bool = True,
        leeway_seconds: int = 30,
    ) -> None:
        if amount_minor is None and not signature_only:
            # subhadipmitra@: Refused at CONSTRUCTION, which is the only place
            # this can be caught before it matters. A validator missing this
            # check still returns 200 for every legitimate request and 403 for
            # an unsigned one, so it looks like it is working — and it would
            # wave through an agent asking for ten times what it was authorised.
            raise MisconfiguredValidator(
                "pass `amount_minor=` so the token can be compared against what "
                "is actually being asked for. Verifying the signature alone "
                "proves some authority existed, not that THIS action was "
                "covered by it. If this resource genuinely has no quantity to "
                "compare, pass signature_only=True and mean it."
            )

        self.app = app
        self.audience = audience
        self.protect = tuple(protect)
        self.amount_minor = amount_minor
        self.jwks_url = jwks_url
        self.signature_only = signature_only
        self.report_replays = report_replays
        self.leeway_seconds = leeway_seconds

        self._keys: dict[str, Any] | None = None
        self._seen = SeenTokens()

    # --- keys -------------------------------------------------------------

    async def _public_keys(self) -> dict[str, Any]:
        """Fetched once, then cached. Verification makes no network call.

        subhadipmitra@: A failure here RAISES rather than returning an empty
        key set. An empty set makes every token fail to verify, which reads as
        "the agents are misbehaving" instead of "this service cannot check
        anything" — the two need opposite responses, and the second is ours.
        """
        if self._keys is None:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(self.jwks_url)
            response.raise_for_status()
            self._keys = public_keys_from_jwks(response.json())
        return self._keys

    # --- ASGI -------------------------------------------------------------

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or not self._guarded(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        body, replay_receive = await _buffer(receive)
        token = _bearer(scope.get("headers") or [])

        if not token:
            await _refuse(send, "no capability token presented")
            return

        try:
            keys = await self._public_keys()
        except Exception as exc:
            # subhadipmitra@: 503, and never 403. We could not CHECK, which is
            # our problem and is transient; refusing as though the agent were
            # unauthorised would send somebody debugging the wrong system.
            logger.error("rotascale: cannot reach the JWK set: %s", exc)
            await _refuse(send, "cannot verify capabilities right now", status=503)
            return

        try:
            claim = verify(token, audience=self.audience, keys=keys,
                           leeway_seconds=self.leeway_seconds)
        except Refused as exc:
            await _refuse(send, str(exc))
            return

        if not self.signature_only:
            asked = self._asked_for(body)
            if asked > claim.amount_minor:
                # The comparison a signature cannot make. The token is genuine;
                # the request exceeds it.
                await _refuse(
                    send,
                    f"authorised for {claim.amount_minor}, asked for {asked}")
                return
        else:
            logger.info(
                "rotascale: %s verified by signature only; the action was not "
                "compared against the token", scope.get("path"))

        # --- replay --------------------------------------------------------
        try:
            cached = self._seen.remember(claim)
        except Refused as exc:
            self._maybe_report(token, scope, "replayed")
            await _refuse(send, str(exc))
            return

        if cached is not None:
            # An honest retry after a lost response gets the SAME answer, not a
            # refusal. Turning one lost packet into a failed payment is how a
            # customer decides to stop using us.
            self._maybe_report(token, scope, "replayed")
            await _replay(send, cached)
            return

        captured = _Capture(send)
        await self.app(scope, replay_receive, captured.send)
        self._seen.record(claim, captured.recorded())

    # --- helpers ----------------------------------------------------------

    def _guarded(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.protect)

    def _asked_for(self, body: bytes) -> int:
        """What this request is actually asking for, per the resource's own rule."""
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        try:
            return int(self.amount_minor(payload) or 0)   # type: ignore[misc]
        except Exception:
            # subhadipmitra@: A body the extractor cannot read is not a zero.
            # Returning 0 would compare "nothing was asked for" against the
            # token and pass — so an unparseable request would be the way
            # around this check. A value nothing can determine must exceed
            # every ceiling.
            logger.warning("rotascale: could not read the requested amount; "
                           "treating it as unbounded")
            return 1 << 62

    def _maybe_report(self, token: str, scope, kind: str) -> None:
        if not self.report_replays:
            return
        client = scope.get("client") or ()
        report_incident(
            token, kind=kind,
            base_url=self.jwks_url.split("/.well-known/")[0],
            source_hint=str(client[0]) if client else None,
        )


# --- ASGI plumbing --------------------------------------------------------


async def _buffer(receive) -> tuple[bytes, Callable[[], Awaitable[dict]]]:
    """Read the whole body, and hand back a `receive` that can serve it again.

    subhadipmitra@: The middleware has to read the body to compare the action,
    and the application still needs it. Consuming it without replaying would
    make every guarded endpoint see an empty request.
    """
    chunks: list[bytes] = []
    more = True
    while more:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        more = message.get("more_body", False)

    body = b"".join(chunks)
    served = False

    async def replay() -> dict:
        nonlocal served
        if not served:
            served = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return body, replay


def _bearer(headers: Iterable[tuple[bytes, bytes]]) -> str:
    for key, value in headers:
        if key.lower() == b"authorization":
            raw = value.decode("latin-1")
            if raw.lower().startswith("bearer "):
                return raw[7:].strip()
    return ""


class _Capture:
    """Records a response so an honest retry can be given the same one."""

    def __init__(self, send) -> None:
        self._send = send
        self._status = 200
        self._headers: list[tuple[bytes, bytes]] = []
        self._body: list[bytes] = []

    async def send(self, message) -> None:
        if message["type"] == "http.response.start":
            self._status = message["status"]
            self._headers = list(message.get("headers") or [])
        elif message["type"] == "http.response.body":
            self._body.append(message.get("body", b""))
        await self._send(message)

    def recorded(self) -> dict:
        return {
            "status": self._status,
            "headers": self._headers,
            "body": b"".join(self._body),
        }


async def _replay(send, recorded: dict) -> None:
    headers = [
        (k, v) for k, v in recorded["headers"]
        if k.lower() not in (b"content-length",)
    ]
    headers.append((b"content-length", str(len(recorded["body"])).encode()))
    # So a caller can tell a replayed answer from a fresh one. Not a refusal —
    # the action happened once and this is the record of it.
    headers.append((b"x-rotascale-replayed", b"true"))
    await send({"type": "http.response.start",
                "status": recorded["status"], "headers": headers})
    await send({"type": "http.response.body", "body": recorded["body"]})


async def _refuse(send, why: str, status: int = 403) -> None:
    payload = json.dumps({"refused": why}).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode())],
    })
    await send({"type": "http.response.body", "body": payload})
