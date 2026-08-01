"""CrewAI middleware — records the hand-off between agents.

    watch_crew(crew)

subhadipmitra@: The governance-interesting thing about CrewAI is not that it
calls models. It is that agents **delegate to each other**, and delegation is a
primitive we already have.

A researcher handing work to a writer is authority moving between principals.
Under `#33` that becomes a child grant whose scope attenuates from the parent's,
and the delegation tree is what makes "who authorised this" answerable three
hops down.

That is not built yet, so this **witnesses** the hand-off and does not pretend
to govern it. Recording a delegation as though it were governed — when nothing
attenuates and nothing would refuse — would be exactly the fabrication this
codebase has been careful to avoid. The step says `governed=False` so nobody
reading the trajectory concludes otherwise.
"""

from typing import Any

from rotascale.client import current_trajectory
from rotascale.middleware._common import logger, truncate


def _name(obj: Any) -> str:
    for attr in ("role", "name", "id"):
        value = getattr(obj, attr, None)
        if value:
            return str(value)
    return type(obj).__name__


class _WatchedCrew:
    def __init__(self, inner: Any, capture_content: bool, limit: int) -> None:
        self._inner = inner
        self._capture = capture_content
        self._limit = limit

    def kickoff(self, *args: Any, **kwargs: Any) -> Any:
        trajectory = current_trajectory()
        if trajectory is not None:
            try:
                trajectory.plan(
                    framework="crewai",
                    agents=[_name(a) for a in getattr(self._inner, "agents", [])],
                    tasks=len(getattr(self._inner, "tasks", []) or []),
                )
            except Exception:
                logger.warning("rotascale: failed to record a crew", exc_info=True)

        result = self._inner.kickoff(*args, **kwargs)

        if trajectory is not None:
            try:
                trajectory.plan(
                    framework="crewai", crew_complete=True,
                    result=truncate(str(result), self._limit) if self._capture else None,
                )
            except Exception:
                logger.warning("rotascale: failed to record a crew result",
                               exc_info=True)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def record_delegation(from_agent: Any, to_agent: Any, task: str | None = None) -> None:
    """Record one agent handing work to another.

    subhadipmitra@: `governed=False` is not a placeholder to tidy up later. It
    is the honest statement that this hand-off was WITNESSED and not authorised
    — no child grant was minted, no scope attenuated, nothing would have
    refused. When `#33` lands this becomes a real delegated grant and the flag
    goes with it.
    """
    trajectory = current_trajectory()
    if trajectory is None:
        return
    try:
        trajectory.delegation(
            _name(to_agent),
            framework="crewai",
            delegated_by=_name(from_agent),
            task=task,
            # See the docstring. Do not remove without making it true.
            governed=False,
            note="witnessed only; authority does not attenuate across this hop yet",
        )
    except Exception:
        logger.warning("rotascale: failed to record a delegation", exc_info=True)


def watch_crew(crew: Any, *, capture_content: bool = True,
               content_limit: int = 2000) -> Any:
    """Wrap a CrewAI crew so its run lands on the trajectory.

    Model calls are recorded by whichever provider middleware wraps the
    underlying client. This adds the crew's own shape: which agents, how many
    tasks, and the result.

    Call `record_delegation(from, to, task)` at a hand-off. It is explicit
    because CrewAI's delegation tool is configurable and guessing at it would
    record hand-offs that did not happen.
    """
    return _WatchedCrew(crew, capture_content, content_limit)
