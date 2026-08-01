"""Google ADK middleware — and the one place a middleware can actually refuse.

    from rotascale.middleware import watch_adk

    with rs.witness("booking-agent") as t:
        watch_adk(agent, grant=GRANT)
        runner.run(...)

subhadipmitra@: ADK is unusual among the frameworks here, and it is worth being
precise about why.

Every other middleware in this package **observes**. It records what the agent
did, and a refusal has to be acted on by the agent's own code calling
`authorize` and honouring the answer. ADK's `before_tool_callback` can return a
value to **short-circuit the call**, which means a `deny` can stop the tool in
the tool path — the agent does not get a vote.

That makes ADK one of exactly two enforcement points we have, the other being
the MCP proxy. It is opt-in: pass `grant=` and tool calls are authorised before
they run; leave it out and this behaves like every other middleware.

Saying that plainly matters. A customer who believes `watch_adk(agent)` alone
enforces anything has bought a control they do not have.
"""

import time
from typing import Any

from rotascale.client import current_trajectory
from rotascale.middleware._common import logger, report_served_model, truncate


class _Watcher:
    def __init__(self, grant: str | None, capture_content: bool, limit: int) -> None:
        self._grant = grant
        self._capture = capture_content
        self._limit = limit
        self._started: dict[str, float] = {}

    # --- model ------------------------------------------------------------

    def before_model(self, callback_context: Any = None,
                     llm_request: Any = None, **kwargs: Any) -> None:
        self._started["model"] = time.perf_counter()
        return None      # None means "carry on" in ADK

    def after_model(self, callback_context: Any = None,
                    llm_response: Any = None, **kwargs: Any) -> None:
        trajectory = current_trajectory()
        if trajectory is None:
            return None

        step: dict[str, Any] = {"provider": "google-adk"}
        started = self._started.pop("model", None)
        if started is not None:
            step["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)

        served = _attr(llm_response, "model") or _attr(llm_response, "model_version")
        if served:
            step["model_served"] = served
            report_served_model(served, "google-adk")

        usage = _attr(llm_response, "usage_metadata")
        if usage is not None:
            step["usage"] = {
                "prompt_tokens": _attr(usage, "prompt_token_count"),
                "completion_tokens": _attr(usage, "candidates_token_count"),
            }
        if self._capture:
            step["response"] = truncate(_text(llm_response), self._limit)

        try:
            trajectory.llm_call(**step)
        except Exception:
            logger.warning("rotascale: failed to record llm_call", exc_info=True)
        return None

    # --- tools: the enforcement point --------------------------------------

    def before_tool(self, tool: Any = None, args: dict | None = None,
                    tool_context: Any = None, **kwargs: Any) -> dict | None:
        """Authorise before the tool runs. A non-None return CANCELS the call.

        subhadipmitra@: This is the whole reason ADK gets its own module. What
        we return here becomes the tool's result, so a refusal never reaches
        the tool — the same property the MCP proxy has, and the difference
        between a control and a note.
        """
        trajectory = current_trajectory()
        name = _attr(tool, "name") or "unknown"

        if trajectory is None or self._grant is None:
            # Observe-only: no grant was supplied, so nothing is enforced.
            if trajectory is not None:
                try:
                    trajectory.tool_call(name, trusted=False, framework="google-adk")
                except Exception:
                    logger.warning("rotascale: failed to record a tool call",
                                   exc_info=True)
            return None

        try:
            decision = trajectory.authorize(
                self._grant, {"tools": [name]},
                raise_on_refusal=False, framework="google-adk")
        except Exception:
            # subhadipmitra@: Enforcement fails CLOSED, unlike capture. An
            # ungoverned action is worse than a delayed one, and returning a
            # refusal here is the only way to stop the call.
            logger.error("rotascale: could not authorise %s; refusing", name,
                         exc_info=True)
            return {"error": "rotascale: authorisation unavailable; the call was "
                             "not permitted to run"}

        if decision.allowed:
            return None

        return {
            "error": f"BLOCKED by Rotascale governance — outcome: "
                     f"{decision.outcome}. {decision.reason}",
            "_rotascale": {"blocked": True, "outcome": decision.outcome},
        }

    def after_tool(self, tool: Any = None, tool_response: Any = None,
                   **kwargs: Any) -> None:
        return None


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _text(response: Any) -> str | None:
    content = _attr(response, "content")
    parts = _attr(content, "parts") or []
    chunks = [_attr(p, "text") for p in parts]
    joined = "".join(c for c in chunks if c)
    return joined or None


def watch_adk(agent: Any, *, grant: str | None = None,
              capture_content: bool = True, content_limit: int = 2000) -> Any:
    """Attach Rotascale to an ADK agent, in place. Returns the agent.

    Without `grant`, this observes: model calls and tool calls land on the
    trajectory and nothing is refused.

    With `grant`, tool calls are **authorised before they run**, and a refusal
    cancels the call — ADK's before-callback can short-circuit, so this is a
    real enforcement point rather than a record of one.
    """
    watcher = _Watcher(grant, capture_content, content_limit)
    agent.before_model_callback = watcher.before_model
    agent.after_model_callback = watcher.after_model
    agent.before_tool_callback = watcher.before_tool
    agent.after_tool_callback = watcher.after_tool
    return agent
