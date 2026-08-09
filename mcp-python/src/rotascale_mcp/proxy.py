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

#: Which tool argument carries the money, per tool (`#152`).
#:
#:     ROTASCALE_MCP_AMOUNT_FIELDS="issue_refund:amount_minor,transfer:value"
#:     ROTASCALE_MCP_AMOUNT_FIELD=amount_minor      # applies to any other tool
#:
#: subhadipmitra@: This exists because the proxy authorized EVERY call with
#: `amount_minor=0`, so an amount budget could never be exhausted by traffic
#: through it. An agent behind the proxy could move any sum against a spent
#: budget, one call at a time, and every call recorded `allow / authorised` —
#: evidence that reads as "the budget was checked and there was room".
#:
#: It was not sloppiness. The proxy deliberately sends argument NAMES and never
#: values, so the amount was excluded along with everything else. That decision
#: is kept: naming the field here sends that ONE value and nothing more.
#:
#: Most agents need none of this. A tool with no money in it — `send_email`,
#: `read_file` — is governed by scope and count budgets, which never needed an
#: amount. The trigger is the GRANT: only one carrying `budget_amount_minor`
#: requires an amount, and only then does an undeclared tool become a problem.
def _amount_fields() -> dict[str, str]:
    fields = {}
    for pair in os.environ.get("ROTASCALE_MCP_AMOUNT_FIELDS", "").split(","):
        tool, _, field = pair.partition(":")
        if tool.strip() and field.strip():
            fields[tool.strip()] = field.strip()
    return fields


#: A tool nobody declared as carrying money. Authorized at 0, honestly, because
#: the operator states which of their tools move money.
UNPRICED = "unpriced"
#: A field WAS declared and the call did not carry a usable number. Refused —
#: declared-but-absent is a real problem, not a free action.
UNRESOLVED = "unresolved"


def amount_for(tool: str, arguments: dict[str, Any]) -> tuple[int, str]:
    """`(amount_minor, source)`. See UNPRICED / UNRESOLVED."""
    field = _amount_fields().get(tool) or os.environ.get(
        "ROTASCALE_MCP_AMOUNT_FIELD", "").strip()
    if not field:
        return 0, UNPRICED
    raw = arguments.get(field)
    if isinstance(raw, bool) or raw is None:
        # bool first: `True` is an int in Python and would authorize for 1.
        return 0, UNRESOLVED
    try:
        return int(raw), field
    except (TypeError, ValueError):
        return 0, UNRESOLVED


def priced(grant: dict) -> bool:
    """Does this grant carry a budget an amount could exhaust?"""
    return bool(grant.get("budget_amount_minor"))


def configured_for(grant: dict) -> bool:
    """Has the operator said which of this grant's tools carry money?

    subhadipmitra@: The distinction that keeps this from becoming an outage.

    An undeclared tool means one of two very different things. If the operator
    has declared nothing at all for this grant, we cannot tell a free action
    from an unpriced one, and the safe reading is that the budget is
    unenforceable — the `#152` case. But once they have named even one money
    tool, silence about the others is a STATEMENT: `read_customer` carries no
    money. Refusing it then would break a working agent to protect a budget it
    was never going to spend.
    """
    if os.environ.get("ROTASCALE_MCP_AMOUNT_FIELD", "").strip():
        return True
    declared = _amount_fields()
    return any(tool in declared
               for tool in (grant.get("scope") or {}).get("tools") or [])


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
        for grant_id, tool in self.unenforceable():
            logger.warning(
                "rotascale: grant %s has a spending budget but '%s' declares no "
                "amount field, so calls to it will be REFUSED. Set "
                "ROTASCALE_MCP_AMOUNT_FIELDS=%s:<argument>.",
                grant_id, tool, tool)
        if not self._grants:
            logger.warning(
                "rotascale: %s holds no authority. Tool calls will be recorded "
                "and %s.", self.agent.slug,
                "REFUSED" if REQUIRE_GRANT else "forwarded",
            )

    def grant_object_for(self, tool: str) -> dict | None:
        for grant in self._grants:
            if tool in ((grant.get("scope") or {}).get("tools") or []):
                return grant
        return None

    def unenforceable(self) -> list[tuple[str, str]]:
        """(grant_id, tool) pairs whose money budget cannot be enforced here.

        subhadipmitra@: A grant with an amount budget whose tools carry no
        declared amount field. The proxy would authorize each call at 0, so the
        budget could never be exhausted — which is exactly the defect `#152`
        found in production, and the reason this is surfaced at startup rather
        than discovered from a ledger afterwards.
        """
        out = []
        for grant in self._grants:
            if not priced(grant):
                continue
            if configured_for(grant):
                continue          # the operator has named their money tools
            for tool in (grant.get("scope") or {}).get("tools") or []:
                out.append((grant["id"], tool))
        return out

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

        grant = self.grant_object_for(tool) or {}
        amount, source = amount_for(tool, arguments)

        # subhadipmitra@: Fail CLOSED, and only here. If this grant carries a
        # money budget and the amount cannot be determined, authorizing at 0
        # would let any sum through against a spent budget — the `#152` defect.
        # Refusing is the enforcement rule the rest of the platform follows, and
        # the message names the one setting that fixes it.
        #
        # Deliberately narrow: it fires only when the grant is priced AND the
        # amount is unknown. A grant with only scope or a count budget never
        # reaches this, which is most agents.
        unenforceable = priced(grant) and (
            source == UNRESOLVED                      # declared, but absent
            or (source == UNPRICED and not configured_for(grant))
        )
        if unenforceable:
            # subhadipmitra@: `grant_id=None`, so the platform RECORDS A
            # REFUSAL rather than an allow.
            #
            # Authorizing against the real grant here returned `allow` — the
            # gate sees a zero-amount action and there is room for it — and then
            # the proxy blocked the call anyway. The ledger then said `allow`
            # for a call that never happened, so a deployment enforcing at the
            # proxy looked, in its own evidence, like one that permitted the
            # spend. That is the same shape as the defect this whole change
            # exists to fix, one level up.
            #
            # Passing no grant is the honest claim: nothing authorised this
            # action, because the proxy could not establish what was being
            # asked for. `#128` made the same call for the REQUIRE_GRANT path.
            await asyncio.to_thread(
                self.client.authorize, None, {"tools": [tool]},
                trajectory_id=self.trajectory.id, raise_on_refusal=False,
                mcp_server=self.server_name,
                mcp_arguments=sorted(arguments)[:20],
                mcp_amount_source=source,
                mcp_refused_by="proxy: spending budget with no amount declared",
            )
            hint = ("no argument named by ROTASCALE_MCP_AMOUNT_FIELDS was "
                    "found on this call"
                    if source == UNRESOLVED else
                    f"set ROTASCALE_MCP_AMOUNT_FIELDS={tool}:<argument> so the "
                    f"amount can be checked against the budget")
            return _blocked(
                message_id, "deny",
                f"authority for '{tool}' carries a spending budget, and this "
                f"proxy cannot tell how much this call would spend — {hint}")

        decision = await asyncio.to_thread(
            self.client.authorize, grant_id, {"tools": [tool]},
            trajectory_id=self.trajectory.id,
            raise_on_refusal=False,
            amount_minor=amount,
            currency=grant.get("budget_currency") or None,
            mcp_server=self.server_name,
            mcp_arguments=sorted(arguments)[:20],   # names only, never values
            # Recorded so a reader can tell "authorised for nothing" from
            # "authorised for the amount asked". Without it both look like 0.
            mcp_amount_source=source,
        )
        if decision.allowed:
            return None
        return _blocked(message_id, decision.outcome, decision.reason)


#: Commands that launch somebody else's server rather than being one.
#: `npx -y @acme/pay-mcp` is a dependency on `@acme/pay-mcp`, not on npx.
_RUNNERS = {"npx", "node", "bunx", "deno", "python", "python3", "uv", "uvx",
            "pipx", "bun", "sh", "bash", "docker"}


def server_name_for(command: list[str]) -> str:
    """A name that identifies the DEPENDENCY, not the interpreter.

    subhadipmitra@: This used to be `command[0]`, so a proxy wrapping
    `npx -y @acme/pay-mcp` registered the dependency as `npx` — and every Node
    MCP server in the estate collapsed into one entry with that name.

    It matters more than it looks. Under DORA an MCP server is an ICT
    third-party dependency and Article 28 requires a register of them (`#151`).
    A register listing `npx` as a third-party dependency is not a register; the
    artefact is only as good as the identity of the things in it.

    `ROTASCALE_MCP_SERVER_NAME` still overrides. Nothing here beats an operator
    naming their own dependency.
    """
    if not command:
        return "unknown"

    head = os.path.basename(command[0])
    if head not in _RUNNERS:
        return head                      # it IS the server

    for argument in command[1:]:
        if argument.startswith("-"):
            continue                     # a flag, not the package
        if argument in ("-m", "run", "exec"):
            continue
        # An npm scoped package survives whole — the `@scope/` IS the
        # identity, and basename would strip the scope and merge two vendors'
        # servers of the same name. Everything else is a path.
        if argument.startswith("@"):
            return argument
        return os.path.basename(argument)
    return head


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
        "ROTASCALE_MCP_SERVER_NAME") or server_name_for(command))

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
