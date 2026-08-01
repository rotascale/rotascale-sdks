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


def split_digests(tools: list[Any]) -> list[dict[str, Any]]:
    """Per-tool description and schema hashes, kept APART.

    subhadipmitra@: `manifest_digest` folds both into one value, which is right
    for "did anything move" but wrong for reporting. A moved schema is a
    version bump; a moved description while the schema holds is the signature
    of tool poisoning — the tool still takes the same arguments, so nothing
    breaks and nobody notices. The server records them separately so an
    operator can tell which happened, and this is what feeds it.
    """
    out: list[dict[str, Any]] = []
    for tool in tools:
        name = str(_attr(tool, "name") or "")
        if not name:
            continue
        description = str(_attr(tool, "description") or "")
        schema = _attr(tool, "inputSchema") or _attr(tool, "input_schema") or {}
        try:
            schema_repr = json.dumps(schema, sort_keys=True, default=str)
        except Exception:
            schema_repr = str(schema)
        out.append({
            "name": name,
            "description_hash": hashlib.sha256(description.encode()).hexdigest(),
            "schema_hash": hashlib.sha256(schema_repr.encode()).hexdigest(),
            "description": description,
        })
    return out


class _WatchedSession:
    def __init__(
        self,
        inner: Any,
        server: str,
        *,
        transport: str | None = None,
        endpoint: str | None = None,
        capture_content: bool = False,
    ) -> None:
        self._inner = inner
        self._server = server
        self._transport = transport
        self._endpoint = endpoint
        self._capture_content = capture_content
        self._digest: str | None = None
        self._per_tool: dict[str, str] = {}
        self._poisoned: set[str] = set()

    async def list_tools(self, *args: Any, **kwargs: Any) -> Any:
        result = await self._inner.list_tools(*args, **kwargs)
        tools = _tools_from(result)
        try:
            self._check_manifest(tools)
        except Exception:
            logger.warning("rotascale: manifest check failed", exc_info=True)
        try:
            self._report_manifest(tools)
        except Exception:
            # Capture never raises. An MCP session must keep working when the
            # control plane is unreachable.
            logger.warning("rotascale: could not report MCP manifest", exc_info=True)
        return result

    def _report_manifest(self, tools: list[Any]) -> None:
        """Send the manifest to Rotascale, so a change is durable.

        subhadipmitra@: `_check_manifest` compares against `self._digest`, which
        lives in this object and dies with the session. That catches an
        injection performed WHILE an agent is connected and misses one performed
        between runs — arguably the easier attack, since nothing is watching.

        Hashes go up always; the descriptions themselves only when the customer
        opted in. A hash is enough to prove something moved, which is the
        control. The text is what lets a human judge whether it mattered, and
        that is their data to share or not.
        """
        trajectory = current_trajectory()
        if trajectory is None or not getattr(trajectory, "id", None):
            return

        payload = split_digests(tools)
        if not self._capture_content:
            for entry in payload:
                entry.pop("description", None)

        trajectory._client._post("/v1/mcp/observe", {
            "server": self._server,
            "transport": self._transport,
            "endpoint": self._endpoint,
            "tools": payload,
            "agent_id": trajectory.agent_id,
            "trajectory_id": trajectory.id,
        })

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


def watch_mcp(
    session: Any,
    *,
    server: str = "default",
    transport: str | None = None,
    endpoint: str | None = None,
    capture_content: bool = False,
) -> Any:
    """Wrap an MCP client session with witnessing and poisoning detection.

    The server and its tools are also reported to Rotascale, so a manifest that
    changes **between** sessions is caught — not only one that changes while an
    agent happens to be connected.

    Args:
        server: what to call this server. Use the name from your MCP host
            config, so a finding is recognisable without cross-referencing.
        transport: ``"stdio"`` or ``"http"``. A local subprocess and a network
            dependency carry different exposure, and only you know which it is.
        endpoint: the command or URL. Recorded as evidence, never used to
            connect.
        capture_content: send tool descriptions as well as their hashes. Off by
            default. Hashes prove a description moved; the text is what lets a
            human judge whether it mattered, and that is yours to share or not.
    """
    return _WatchedSession(
        session, server, transport=transport, endpoint=endpoint,
        capture_content=capture_content,
    )
