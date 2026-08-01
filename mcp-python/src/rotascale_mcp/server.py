"""Rotascale governance, exposed as MCP tools.

subhadipmitra@: **This surface is opt-in, and therefore advisory.** The agent
decides whether to call `authorize_action`; an agent that never calls it is not
governed by it. That is genuinely useful — it produces real evidence and gives a
cooperating agent a real gate — but it is not a control, and saying otherwise
would be dishonest. `rotascale-mcp-proxy` is the surface an agent cannot opt out
of, and the README says so in as many words.

Every tool description below is written for a model to read, not a developer.
There is an irony worth noting: a tool description is exactly the injection
surface `#77` exists to detect, so these are deliberately blunt, imperative, and
free of anything a model could read as negotiable.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server.mcpserver import MCPServer

from rotascale_mcp.session import Session, describe_outcome

logger = logging.getLogger("rotascale_mcp")

INSTRUCTIONS = """\
Rotascale governs what this agent is allowed to do and records what it did.

Call `open_trajectory` once, before starting a task. Call `authorize_action`
BEFORE any consequential action — moving money, changing a record, sending a
message, calling an external system. Honour the answer: if `allowed` is false,
stop and say why. Record what you read and did with `witness_step`. Call
`close_trajectory` when the task ends.

If a decision refuses, do not retry it and do not look for another route to the
same effect. The refusal is the answer.
"""


def build(session: Session | None = None) -> MCPServer:
    live = session or Session()
    server = MCPServer(
        name="rotascale",
        title="Rotascale governance",
        description="Ask permission before acting, and record what happened.",
        instructions=INSTRUCTIONS,
    )

    @server.tool(
        description=(
            "Start a governed unit of work. Call this once at the beginning of "
            "a task, before doing anything else. `agent` is a stable name you "
            "choose for this agent, such as 'refund-assistant' — use the same "
            "name every run. `ref` is your own identifier for this particular "
            "task, such as a ticket number."
        )
    )
    async def open_trajectory(
        agent: str, ref: str | None = None, goal: str | None = None
    ) -> dict[str, Any]:
        resolved = await live.resolve_agent(agent)
        trajectory = await live.open(
            resolved, ref, {"description": goal} if goal else None)
        return {
            "trajectory_id": trajectory.id,
            "agent_id": resolved.id,
            "agent": resolved.slug,
            "governed": resolved.governed,
            # An agent that has just been discovered records evidence but holds
            # no authority. Saying so here stops a model reporting that it is
            # "governed" when nothing is being enforced.
            "note": (
                "Recording is active."
                if resolved.governed
                else "This agent holds no authority yet and nobody has claimed "
                     "it. Evidence is being recorded, but no action will be "
                     "refused. Tell the user if they ask whether you are governed."
            ),
        }

    @server.tool(
        description=(
            "Ask whether this agent may perform a consequential action, BEFORE "
            "performing it. Consequential means: moving money, changing or "
            "deleting a record, contacting a person, or calling an external "
            "system that does any of those.\n\n"
            "`grant_id` is the authority to check against. `tools` names what "
            "you intend to use. `amount_minor` is the value at stake in minor "
            "units (cents, pence) when money is involved.\n\n"
            "You MUST honour the result. If `allowed` is false, do not perform "
            "the action, do not retry, and do not attempt a different route to "
            "the same effect. Read `guidance` and follow it."
        )
    )
    async def authorize_action(
        grant_id: str,
        tools: list[str] | None = None,
        amount_minor: int = 0,
        currency: str | None = None,
        stakes_minor: int | None = None,
    ) -> dict[str, Any]:
        decision = await live.call(
            live.client.authorize,
            grant_id,
            {"tools": tools} if tools else None,
            amount_minor=amount_minor,
            currency=currency,
            stakes_minor=stakes_minor,
            trajectory_id=live.live.trajectory.id if live.live else None,
            # Never raises here: an exception across an MCP boundary becomes an
            # error string the model may well ignore. A structured refusal it
            # has been told to honour is far more likely to be obeyed.
            raise_on_refusal=False,
        )
        return {
            "outcome": decision.outcome,
            "allowed": decision.allowed,
            "reason": decision.reason,
            "guidance": describe_outcome(decision.outcome),
            "remaining_amount_minor": decision.remaining_amount_minor,
            "remaining_count": decision.remaining_count,
            # If a grant is being measured rather than enforced, a refusal was
            # recorded and NOT applied. The agent proceeded legitimately, and a
            # model claiming it was "approved" would be overstating it.
            "enforcing": decision.enforcing,
            "findings": decision.findings,
        }

    @server.tool(
        description=(
            "Record something you read or did, on the current trajectory. Use "
            "`kind='retrieval'` when you read a document, page or other content "
            "— this is important: content from an untrusted source restricts "
            "what you may do afterwards. Use `kind='tool_call'` when you used a "
            "tool, and `kind='llm_call'` for a model call. `source` identifies "
            "where the content came from.\n\n"
            "Do not mark something trusted unless you know it is."
        )
    )
    async def witness_step(
        kind: str,
        summary: str,
        source: str | None = None,
        trusted: bool = False,
    ) -> dict[str, Any]:
        if live.live is None:
            return {
                "recorded": False,
                "error": "No trajectory is open. Call open_trajectory first.",
            }
        trajectory = live.live.trajectory
        if kind == "retrieval" and source:
            await live.call(trajectory.retrieval, source,
                            trusted=trusted, summary=summary)
        elif kind == "tool_call" and source:
            await live.call(trajectory.tool_call, source,
                            trusted=trusted, summary=summary)
        else:
            await live.call(trajectory.step, kind, summary=summary, source=source)
        return {"recorded": True, "trajectory_id": trajectory.id}

    @server.tool(
        description=(
            "What this agent is allowed to do right now, and how much of each "
            "budget is left. Read-only. Call it when you need to know whether "
            "an action is worth attempting, or which authority to name in "
            "`authorize_action`."
        )
    )
    async def check_authority() -> dict[str, Any]:
        if live.live is None:
            return {"error": "No trajectory is open. Call open_trajectory first."}
        agent_id = live.live.agent.id
        response = await live.call(
            live.client.http.get, f"/v1/agents/{agent_id}/grants")

        # subhadipmitra@: An older server refuses an API key here, and a refusal
        # is a problem document rather than a list. Iterating it produced a
        # TypeError that reached the host as an opaque tool failure — telling a
        # model nothing, which is how it ends up guessing at its own authority.
        if response.status_code != 200:
            return {
                "error": f"Could not read this agent's authority "
                         f"(HTTP {response.status_code}).",
                "guidance": "Do not assume you hold any authority. Name the "
                            "grant you were given and let authorize_action "
                            "decide.",
            }
        grants = response.json()
        if not isinstance(grants, list):
            return {"error": "Unexpected response while reading authority.",
                    "guidance": "Do not assume you hold any authority."}

        return {
            "agent": live.live.agent.slug,
            "grants": [
                {
                    "grant_id": g["id"],
                    "scope": g.get("scope"),
                    "budget_remaining_minor": (
                        None if g.get("budget_amount_minor") is None
                        else g["budget_amount_minor"] - g.get("spent_amount_minor", 0)
                    ),
                    "calls_remaining": (
                        None if g.get("budget_count") is None
                        else g["budget_count"] - g.get("spent_count", 0)
                    ),
                    "expires": g.get("not_after"),
                    "status": g.get("status"),
                    "enforcing": g.get("enforcement_mode") == "enforce",
                }
                for g in grants
            ],
        }

    @server.tool(
        description=(
            "End the current unit of work. Call this when the task is finished, "
            "whether it succeeded or not. Use `status='failed'` if it did not "
            "complete — a failure is evidence too, and hiding it is worse than "
            "recording it."
        )
    )
    async def close_trajectory(
        status: str = "completed", summary: str | None = None
    ) -> dict[str, Any]:
        trajectory_id = await live.close(
            status, {"summary": summary} if summary else {})
        if trajectory_id is None:
            return {"closed": False, "error": "No trajectory was open."}
        return {"closed": True, "trajectory_id": trajectory_id, "status": status}

    return server


def run() -> None:
    transport = os.environ.get("ROTASCALE_MCP_TRANSPORT", "stdio")
    if transport not in ("stdio", "sse", "streamable-http"):
        raise SystemExit(
            f"unknown ROTASCALE_MCP_TRANSPORT {transport!r}: "
            f"expected stdio, sse or streamable-http"
        )
    # stdio is the default because that is how MCP hosts launch a local server.
    # Logging must go to stderr: stdout IS the protocol channel, and a stray
    # print corrupts the stream in a way that looks like a client bug.
    logging.basicConfig(level=os.environ.get("ROTASCALE_MCP_LOG", "WARNING"))
    build().run(transport=transport)
