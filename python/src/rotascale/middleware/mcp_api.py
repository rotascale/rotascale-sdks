"""MCP middleware — and the tool-poisoning detector.

Wraps an MCP client session so tool discovery and tool calls land on the
trajectory. Duck-typed: imports no MCP SDK.

The part that matters is the manifest digest. A malicious or compromised MCP
server can rewrite a tool's *description* mid-session to inject instructions —
the agent re-reads the tool list, the description now says something new, and
the model follows it. Nothing about the transcript looks wrong, because the
attack lives in metadata nobody records.

    session = watch_mcp(session)

A description or schema change between two `list_tools()` calls in one session
raises a finding on the trajectory AND taints the context, so any grant
requiring a clean context stops the agent before it can act on the injection.
"""

import hashlib
import json
import time
from typing import Any

from rotascale.client import current_trajectory
from rotascale.middleware._common import logger


def _attr(obj: Any, name: str) -> Any:
    """MCP SDKs and plain dicts both turn up in the wild."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _tools_from(result: Any) -> list[Any]:
    tools = _attr(result, "tools")
    if tools is None and isinstance(result, list | tuple):
        tools = result
    return list(tools or [])


def manifest_digest(tools: list[Any]) -> tuple[str, dict[str, str]]:
    """SHA-256 over each tool's (name, description, input schema).

    subhadipmitra@: The description and schema are IN the digest deliberately —
    they are the whole attack surface. Hashing names alone would miss the entire
    tool-poisoning class, because the tool list looks identical while the
    instructions attached to it have changed underneath.

    Returns the overall digest plus a per-tool digest, so a finding can name
    which tool changed rather than only that something did.
    """
    per_tool: dict[str, str] = {}
    for tool in tools:
        name = str(_attr(tool, "name") or "")
        description = str(_attr(tool, "description") or "")
        schema = _attr(tool, "inputSchema") or _attr(tool, "input_schema") or {}
        try:
            schema_repr = json.dumps(schema, sort_keys=True, default=str)
        except Exception:
            schema_repr = str(schema)
        per_tool[name] = hashlib.sha256(
            f"{name}\x1f{description}\x1f{schema_repr}".encode()
        ).hexdigest()

    combined = hashlib.sha256(
        "\x1e".join(f"{n}:{d}" for n, d in sorted(per_tool.items())).encode()
    ).hexdigest()
    return combined, per_tool


class _WatchedSession:
    def __init__(self, inner: Any, server: str) -> None:
        self._inner = inner
        self._server = server
        self._digest: str | None = None
        self._per_tool: dict[str, str] = {}
        self._poisoned: set[str] = set()

    async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._inner.list_tools(*args, **kwargs)
        try:
            self._check_manifest(_tools_from(result))
        except Exception:
            logger.warning("rotascale: manifest check failed", exc_info=True)
        return result

    def _check_manifest(self, tools: list[Any]) -> None:
        digest, per_tool = manifest_digest(tools)
        trajectory = current_trajectory()

        if self._digest is None:
            self._digest, self._per_tool = digest, per_tool
            if trajectory is not None:
                trajectory.tool_call(
                    f"mcp:{self._server}:list_tools",
                    trusted=True,          # the first listing is the baseline
                    manifest_digest=digest,
                    tool_count=len(per_tool),
                    tools=sorted(per_tool),
                )
            return

        if digest == self._digest:
            return

        changed = sorted(
            name for name, d in per_tool.items()
            if self._per_tool.get(name) not in (None, d)
        )
        added = sorted(set(per_tool) - set(self._per_tool))
        removed = sorted(set(self._per_tool) - set(per_tool))
        self._poisoned.update(changed)
        self._digest, self._per_tool = digest, per_tool

        logger.error(
            "rotascale: MCP manifest changed mid-session on %s — changed=%s added=%s removed=%s",
            self._server, changed, added, removed,
        )
        if trajectory is not None:
            # Recorded as an UNTRUSTED retrieval, so it taints the context. A
            # grant requiring a clean context now refuses to act — the injection
            # is stopped rather than merely noted after the fact.
            trajectory.retrieval(
                f"mcp:{self._server}:manifest_changed",
                finding="mcp_manifest_changed",
                changed_tools=changed,
                added_tools=added,
                removed_tools=removed,
                previous_digest=self._digest,
            )

    async def call_tool(self, name: str, arguments: Any = None, *args: Any, **kwargs: Any) -> Any:
        trajectory = current_trajectory()
        started = time.perf_counter()
        poisoned = name in self._poisoned
        try:
            result = await self._inner.call_tool(name, arguments, *args, **kwargs)
        except Exception as exc:
            if trajectory is not None:
                trajectory.tool_call(
                    f"mcp:{self._server}:{name}",
                    latency_ms=round((time.perf_counter() - started) * 1000, 1),
                    error=type(exc).__name__,
                    poisoned=poisoned,
                )
            raise
        if trajectory is not None:
            trajectory.tool_call(
                f"mcp:{self._server}:{name}",
                # Argument KEYS only — values are the agent's data, and this is
                # an evidence store, not a copy of the customer's database.
                argument_keys=sorted(arguments) if isinstance(arguments, dict) else None,
                latency_ms=round((time.perf_counter() - started) * 1000, 1),
                poisoned=poisoned,
            )
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def watch_mcp(session: Any, *, server: str = "default") -> Any:
    """Wrap an MCP client session with witnessing and poisoning detection."""
    return _WatchedSession(session, server)
