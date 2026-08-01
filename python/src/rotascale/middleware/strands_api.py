"""AWS Strands middleware.

    watch_strands(agent)

subhadipmitra@: Two things about Strands are worth building for.

**The tool registry is readable at wrap time.** Strands registers tools as
decorated Python functions, so the manifest is right there — no digest for a
human to retype, which is what `#59` is about. It is reported as provenance the
moment the agent is wrapped.

**The loop is model-driven and unbounded.** Strands runs the model until the
model decides it is done. That is exactly the shape a CALL budget exists to
bound, and it makes a Strands agent the clearest demonstration of `exhausted`
as an outcome distinct from `deny`: nothing is out of scope, the agent has
simply spent what it was given.
"""

from typing import Any

from rotascale.client import current_trajectory
from rotascale.middleware._common import logger


def _tool_manifest(agent: Any) -> dict[str, Any]:
    """What this agent can do, read off the registry rather than declared."""
    tools = getattr(agent, "tools", None) or getattr(agent, "tool_registry", None) or []
    if isinstance(tools, dict):
        tools = list(tools.values())
    manifest = {}
    for tool in tools:
        name = (getattr(tool, "tool_name", None) or getattr(tool, "name", None)
                or getattr(tool, "__name__", None))
        if not name:
            continue
        # Description included: for an agent, a tool's description is an
        # instruction the model reads, and a change to it changes behaviour.
        manifest[str(name)] = (getattr(tool, "tool_spec", None)
                               or getattr(tool, "__doc__", None) or "")
    return manifest


class _WatchedAgent:
    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        trajectory = current_trajectory()
        if trajectory is not None:
            try:
                trajectory.plan(framework="aws-strands", invocation=True)
            except Exception:
                logger.warning("rotascale: failed to record an invocation",
                               exc_info=True)
        return self._inner(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def watch_strands(agent: Any) -> Any:
    """Wrap a Strands agent, reporting its tool manifest as provenance.

    The manifest is read from the registry at wrap time — the runtime already
    knows what it can do, and a human retyping that would be recording a guess.
    """
    trajectory = current_trajectory()
    manifest = _tool_manifest(agent)
    if trajectory is not None and manifest:
        try:
            trajectory._client.report_provenance(
                trajectory.agent_id, tools=manifest)
        except Exception:
            logger.warning("rotascale: could not report a Strands manifest",
                           exc_info=True)
    return _WatchedAgent(agent)
