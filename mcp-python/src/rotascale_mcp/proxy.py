"""A transparent MCP proxy: the surface an agent cannot opt out of.

subhadipmitra@: `rotascale-mcp` (server.py) gives an agent governance tools it
may choose to call. This sits *between* the agent and the MCP server it already
uses, so every tool call passes through whether the agent likes it or not. That
is the difference between a suggestion and a control, and it is why this file
exists separately.

    agent / MCP host  <--stdio-->  rotascale-mcp-proxy  <--stdio-->  real server

Two rules shape everything below.

**Transparency.** Anything not being governed is forwarded as the exact bytes
that arrived. Re-serialising JSON would reorder keys and change nothing
semantically, but MCP servers are written by many hands and one of them will
depend on something we did not think about. Bytes in, bytes out.

**The relay must never wedge.** If governance itself fails — Rotascale
unreachable, a bug here — the proxy logs and forwards. A governance layer that
takes down the customer's agent has caused the outage it was bought to prevent.
The single exception is an authorisation that came back refused: that is the
control working, and it stops the call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from rotascale import Agent, Rotascale, Trajectory
from rotascale.middleware.mcp_api import manifest_digest, split_digests

from rotascale_mcp.session import describe_outcome

logger = logging.getLogger("rotascale_mcp.proxy")

#: Refuse any tool call no grant covers, rather than recording and forwarding.
#: Off by default: switching it on turns an unconfigured deployment into a
#: broken one, and the finding is visible in the console either way.
REQUIRE_GRANT = os.environ.get("ROTASCALE_MCP_REQUIRE_GRANT", "").lower() in (
    "1", "true", "yes")


def _blocked(message_id: Any, outcome: str, reason: str) -> dict:
    """The reply an agent gets instead of its tool result.

    subhadipmitra@: A tool RESULT with `isError`, not a JSON-RPC error. A
    protocol-level error is often swallowed by the host and shown to the model
    as "the tool failed", which invites a retry. A result puts our sentence in
    front of the model, where it can be read and obeyed.
    """
    text = (
        f"BLOCKED by Rotascale governance — outcome: {outcome}.\n"
        f"{reason}\n\n{describe_outcome(outcome)}"
    )
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": True,
            "_rotascale": {"blocked": True, "outcome": outcome, "reason": reason},
        },
    }


class Governor:
    """Holds the agent, its grants, and the trajectory for this session."""

    def __init__(self, client: Rotascale, agent: Agent, server_name: str) -> None:
        self.client = client
        self.agent = agent
        self.server_name = server_name
        self.trajectory: Trajectory | None = None
        self._grants: list[dict] = []
        self._digest: str | None = None

    async def start(self, ref: str | None) -> None:
        created = await asyncio.to_thread(
            self.client._post, "/v1/trajectories",
            {"agent_id": self.agent.id, "external_ref": ref,
             "goal": {"mcp_server": self.server_name}},
        )
        self.trajectory = Trajectory(self.client, created["id"], self.agent.id)
        await self.refresh_grants()

    async def refresh_grants(self) -> None:
        response = await asyncio.to_thread(
            self.client.http.get, f"/v1/agents/{self.agent.id}/grants")
        self._grants = response.json() if response.status_code == 200 else []
        if not self._grants:
            logger.warning(
                "rotascale: %s holds no authority. Tool calls will be recorded "
                "and %s.", self.agent.slug,
                "REFUSED" if REQUIRE_GRANT else "forwarded",
            )

    def grant_for(self, tool: str) -> str | None:
        """The first grant whose scope names this tool.

        subhadipmitra@: Deliberately exact-match rather than prefix or glob. A
        scope is a statement about what somebody authorised, and inferring that
        `file_*` covers `file_delete` is the platform putting words in a human's
        mouth. If a wildcard is wanted it should be an explicit scope feature,
        decided once, not an accident of matching here.
        """
        for grant in self._grants:
            tools = (grant.get("scope") or {}).get("tools") or []
            if tool in tools:
                return grant["id"]
        return None

    async def observe_manifest(self, tools: list[dict]) -> None:
        """Report the downstream server's manifest, and taint on a change."""
        if self.trajectory is None:
            return
        digest, _ = manifest_digest(tools)
        payload = split_digests(tools)
        try:
            result = await asyncio.to_thread(
                self.client._post, "/v1/mcp/observe",
                {"server": self.server_name, "transport": "stdio",
                 "tools": payload, "agent_id": self.agent.id,
                 "trajectory_id": self.trajectory.id},
            )
        except Exception:
            logger.warning("rotascale: could not report manifest", exc_info=True)
            return

        changed = self._digest is not None and digest != self._digest
        self._digest = digest
        if result.get("injection_risk") or changed:
            # Recorded as an UNTRUSTED retrieval so it taints the trajectory: a
            # grant needing a clean context now refuses the next privileged
            # action. The injection is stopped, not merely noted afterwards.
            await asyncio.to_thread(
                self.trajectory.retrieval,
                f"mcp:{self.server_name}:manifest_changed",
                finding="mcp_manifest_changed",
                injection_risk=bool(result.get("injection_risk")),
                changes=[c.get("kind") for c in result.get("changes", [])],
            )
            logger.error(
                "rotascale: MCP manifest changed on %s (injection_risk=%s)",
                self.server_name, result.get("injection_risk"),
            )

    async def decide(self, message_id: Any, tool: str, arguments: dict) -> dict | None:
        """None to forward the call; a reply dict to stop it."""
        if self.trajectory is None:
            return None

        grant_id = self.grant_for(tool)
        if grant_id is None:
            await asyncio.to_thread(
                self.trajectory.tool_call, tool, ungoverned=True,
                note="no grant covers this tool")
            if REQUIRE_GRANT:
                # subhadipmitra@: RECORDED as a decision, not refused locally
                # (`#128`).
                #
                # This used to answer here and write nothing to the ledger, so
                # the strongest refusal the product makes — a tool call stopped
                # on the wire, which the agent cannot route around — was absent
                # from every count of refusals, including the assurance file. A
                # deployment enforcing hard at the proxy looked, in its own
                # evidence, like one refusing nothing.
                #
                # Recorded with NO grant, which is the honest shape: the action
                # was refused, and the reason is that nothing authorised it.
                await asyncio.to_thread(
                    self.client.authorize, None, {"tools": [tool]},
                    trajectory_id=self.trajectory.id,
                    raise_on_refusal=False,
                    mcp_server=self.server_name,
                    mcp_arguments=sorted(arguments)[:20],
                )
                return _blocked(
                    message_id, "deny",
                    f"No authority covers the tool {tool!r}, and this "
                    f"deployment requires one.")
            return None

        decision = await asyncio.to_thread(
            self.client.authorize, grant_id, {"tools": [tool]},
            trajectory_id=self.trajectory.id,
            raise_on_refusal=False,
            mcp_server=self.server_name,
            mcp_arguments=sorted(arguments)[:20],   # names only, never values
        )
        if decision.allowed:
            return None
        return _blocked(message_id, decision.outcome, decision.reason)


def _parse(line: bytes) -> dict | None:
    try:
        message = json.loads(line)
        return message if isinstance(message, dict) else None
    except Exception:
        return None


def _tools_from_result(message: dict) -> list[dict]:
    result = message.get("result") or {}
    tools = result.get("tools")
    return tools if isinstance(tools, list) else []


async def run(agent_slug: str, command: list[str], *, ref: str | None = None) -> int:
    client = Rotascale(
        os.environ.get("ROTASCALE_URL"),
        api_key=os.environ.get("ROTASCALE_API_KEY"),
    )
    agent = await asyncio.to_thread(client.agent, agent_slug)
    governor = Governor(client, agent, server_name=os.environ.get(
        "ROTASCALE_MCP_SERVER_NAME", command[0]))

    try:
        await governor.start(ref)
    except Exception:
        # Capture fails open. Without a trajectory nothing is recorded, but the
        # customer's agent still runs — which is the right trade even though it
        # means this session is ungoverned. It is logged loudly.
        logger.error("rotascale: could not open a trajectory; relaying "
                     "UNGOVERNED", exc_info=True)

    server = await asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
    )
    loop = asyncio.get_running_loop()
    agent_reader = asyncio.StreamReader()
    await loop.connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(agent_reader), os.fdopen(0, "rb"))
    agent_writer_transport, agent_writer_protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, os.fdopen(1, "wb"))
    agent_writer = asyncio.StreamWriter(
        agent_writer_transport, agent_writer_protocol, None, loop)

    async def reply(message: dict) -> None:
        agent_writer.write(json.dumps(message).encode() + b"\n")
        await agent_writer.drain()

    async def agent_to_server() -> None:
        while True:
            line = await agent_reader.readline()
            if not line:
                break
            message = _parse(line)
            if message is not None and message.get("method") == "tools/call":
                try:
                    params = message.get("params") or {}
                    blocked = await governor.decide(
                        message.get("id"), params.get("name", ""),
                        params.get("arguments") or {})
                    if blocked is not None:
                        await reply(blocked)
                        continue        # never reaches the downstream tool
                except Exception:
                    # Never let a governance bug wedge the relay. The call goes
                    # through ungoverned, loudly, rather than the agent hanging.
                    logger.error("rotascale: enforcement failed; forwarding",
                                 exc_info=True)
            server.stdin.write(line)
            await server.stdin.drain()
        if server.stdin.can_write_eof():
            server.stdin.write_eof()

    async def server_to_agent() -> None:
        while True:
            line = await server.stdout.readline()
            if not line:
                break
            message = _parse(line)
            if message is not None:
                tools = _tools_from_result(message)
                if tools:
                    try:
                        await governor.observe_manifest(tools)
                    except Exception:
                        logger.warning("rotascale: manifest check failed",
                                       exc_info=True)
            # Forwarded verbatim: the agent sees exactly what the server sent.
            agent_writer.write(line)
            await agent_writer.drain()

    await asyncio.gather(agent_to_server(), server_to_agent())

    if governor.trajectory is not None:
        try:
            await asyncio.to_thread(governor.trajectory.close, "completed")
        except Exception:
            logger.warning("rotascale: could not close the trajectory",
                           exc_info=True)
    return await server.wait()
