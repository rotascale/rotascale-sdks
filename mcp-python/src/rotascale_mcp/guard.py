"""Make an MCP server refuse a tool call that was not authorised.

subhadipmitra@: This is the other end of the wire from `proxy.py`, and the
difference between them is the whole point of `#96`.

The PROXY sits between the agent and the server, reads the tool name off the
wire and asks Rotascale. It is a real enforcement point and it has one gap the
epic is explicit about: it is *bypassable by not using that tool*. An agent that
talks to the MCP server directly, or a second agent nobody routed through the
proxy, never meets it.

This GUARD lives in the server. It verifies a capability token offline, with a
cached public key and no call to Rotascale, and refuses without one. There is
nothing to route around: the tool does not execute.

    from rotascale_mcp.guard import CapabilityGuard

    guard = CapabilityGuard(
        audience="mcp://contracts.acme.internal",
        amounts={"settle_payment": lambda args: args["amount_minor"]},
    )

    async def call_tool(name, arguments):
        arguments = guard.check(name, arguments)   # raises Refused, or returns
        return await tools[name](**arguments)      # cleaned of the token

## Where the token travels

MCP has no headers on stdio, so it goes in the request. `_meta` is the correct
place — it is the protocol's own slot for out-of-band data — and the guard also
accepts a reserved key inside `arguments` because plenty of clients cannot set
`_meta` yet.

Either way it is **stripped before the tool sees it**. A tool whose signature
grew a `_rotascale_capability` parameter because of us is a tool we broke.

## What a refusal looks like

A tool RESULT with `isError`, not a JSON-RPC error — the same choice `proxy.py`
made and for the same reason. A protocol error is usually swallowed by the host
and shown to the model as "the tool failed", which invites a retry. A result
puts the sentence in front of the model, where it can be read and obeyed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from rotascale.capability import Refused, SeenTokens, public_keys_from_jwks, verify

logger = logging.getLogger("rotascale_mcp.guard")

__all__ = ["CapabilityGuard", "Refused", "refusal_result"]

DEFAULT_JWKS_URL = "https://api.rotascale.com/.well-known/jwks.json"

#: Where a client puts the token when it cannot set `_meta`.
ARGUMENT_KEY = "_rotascale_capability"
#: The protocol's own slot, and the preferred one.
META_KEY = "rotascale/capability"


class MisconfiguredGuard(ValueError):
    """The guard was set up in a way that would not actually guard anything."""


def refusal_result(message_id: Any, why: str) -> dict:
    """The JSON-RPC reply an unauthorised call gets."""
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {
            "content": [{
                "type": "text",
                "text": (f"REFUSED by this server — {why}\n\n"
                         f"This tool requires a capability token from Rotascale "
                         f"naming this action. Ask for authorisation and present "
                         f"the token it returns."),
            }],
            "isError": True,
            "_rotascale": {"refused": True, "reason": why, "enforced_at": "resource"},
        },
    }


class CapabilityGuard:
    """Verify a capability token before an MCP tool runs.

    Parameters
    ----------
    audience:
        This server's own identifier, as the grant names it. A token minted for
        anything else is refused — what stops a credential for one MCP server
        being replayed against another.
    protect:
        Tool names that require a token. `None` means every tool, which is the
        safer default: a server that lists its protected tools will forget one.
    amounts:
        Per-tool: given the call's arguments, what is ACTUALLY being asked for.
        A tool that moves a quantity must have one, or the guard refuses to
        start — see below.
    quantityless:
        Tools where the token's existence IS the whole authorisation, because
        there is no quantity to compare. Named explicitly, so it is a decision
        rather than an omission.
    """

    def __init__(
        self,
        *,
        audience: str,
        protect: list[str] | None = None,
        amounts: dict[str, Callable[[dict[str, Any]], int]] | None = None,
        quantityless: list[str] | None = None,
        jwks_url: str = DEFAULT_JWKS_URL,
        leeway_seconds: int = 30,
    ) -> None:
        self.audience = audience
        self.protect = protect
        self.amounts = dict(amounts or {})
        self.quantityless = set(quantityless or [])
        self.jwks_url = jwks_url
        self.leeway_seconds = leeway_seconds

        overlap = set(self.amounts) & self.quantityless
        if overlap:
            raise MisconfiguredGuard(
                f"{sorted(overlap)} are listed as both having an amount to "
                f"compare and having none. One of the two is wrong, and "
                f"guessing which would decide how much authority they need.")

        self._keys: dict[str, Any] | None = None
        self._seen = SeenTokens()

    # --- keys -------------------------------------------------------------

    def load_keys(self, jwks: dict[str, Any]) -> None:
        """Supply the JWK set directly, for a server that fetches it itself."""
        self._keys = public_keys_from_jwks(jwks)

    def _public_keys(self) -> dict[str, Any]:
        if self._keys is None:
            import urllib.request

            if not self.jwks_url.startswith(("http://", "https://")):
                raise ValueError("the JWKS url must be http(s)")
            with urllib.request.urlopen(  # noqa: S310 — scheme checked above
                    self.jwks_url, timeout=10) as response:
                import json

                self._keys = public_keys_from_jwks(json.load(response))
        return self._keys

    # --- the check --------------------------------------------------------

    def check(self, tool: str, arguments: dict[str, Any],
              meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Refuse an unauthorised call, or return the arguments to run with.

        Raises `Refused`. Returns the arguments with the token removed, so the
        tool never sees a parameter it did not declare.
        """
        if not self._guarded(tool):
            return arguments

        token, cleaned = _extract(arguments, meta)
        if not token:
            raise Refused("no capability token presented")

        try:
            keys = self._public_keys()
        except Exception as exc:
            # subhadipmitra@: Distinguished from a refusal on purpose. We could
            # not CHECK, which is this server's problem and is transient;
            # reporting it as "not authorised" would send somebody debugging the
            # agent instead of the network.
            raise Refused(
                f"this server cannot verify capabilities right now ({exc})"
            ) from exc

        claim = verify(token, audience=self.audience, keys=keys,
                       leeway_seconds=self.leeway_seconds)

        # The tool named in the token must be the tool being called. Without
        # this a token for a cheap read would authorise an expensive write on
        # the same server, since `aud` only names the server.
        if claim.tool and claim.tool != tool:
            raise Refused(
                f"this token authorises {claim.tool!r}, and {tool!r} was called")

        if tool in self.amounts:
            asked = self._asked_for(tool, cleaned)
            if asked > claim.amount_minor:
                # The comparison a signature cannot make: the credential is
                # genuine and the request exceeds it.
                raise Refused(
                    f"authorised for {claim.amount_minor}, asked for {asked}")
        elif tool not in self.quantityless:
            # subhadipmitra@: Refused at CALL time rather than silently allowed,
            # because a protected tool with no rule is one nobody decided about.
            # Listing it in `quantityless` takes two seconds; discovering later
            # that it never compared anything takes an incident.
            raise Refused(
                f"{tool!r} is protected but this server declares no way to "
                f"compare what it was asked for. Add it to `amounts`, or to "
                f"`quantityless` if it genuinely has no quantity.")

        # Replay. The token is a bearer credential, so a second presentation
        # must not act again.
        #
        # subhadipmitra@: Deliberately NOT followed by `record()`. The HTTP
        # guard records the response so an honest retry is answered rather than
        # refused; there is no MCP result to hand back from here, and recording
        # `None` as the outcome would make the second presentation
        # indistinguishable from the first — which is the exact hole
        # `SeenTokens._IN_FLIGHT` exists to close, defeated by using it wrongly.
        #
        # Leaving it in flight means every later presentation raises, which is
        # the honest behaviour for a server that cannot replay its own answer,
        # and the reason two-phase settlement exists for anything consequential.
        self._seen.remember(claim)
        return cleaned

    # --- helpers ----------------------------------------------------------

    def _guarded(self, tool: str) -> bool:
        return True if self.protect is None else tool in self.protect

    def _asked_for(self, tool: str, arguments: dict[str, Any]) -> int:
        try:
            return int(self.amounts[tool](arguments) or 0)
        except Exception:
            # A value nothing can determine must exceed every ceiling.
            # Returning 0 would make a malformed call the way around the check.
            logger.warning(
                "rotascale: could not read the amount for %r; treating it as "
                "unbounded", tool)
            return 1 << 62


def _extract(arguments: dict[str, Any],
             meta: dict[str, Any] | None) -> tuple[str, dict[str, Any]]:
    """Pull the token out, and hand back arguments the tool will recognise."""
    token = ""
    if meta:
        token = str(meta.get(META_KEY) or "")
    cleaned = dict(arguments or {})
    if not token:
        token = str(cleaned.pop(ARGUMENT_KEY, "") or "")
    else:
        cleaned.pop(ARGUMENT_KEY, None)
    return token, cleaned
