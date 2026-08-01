"""AutoGen / AG2 middleware — records turns, and the runaway.

    watch_autogen(groupchat)

subhadipmitra@: AutoGen is conversation-shaped. Agents talk to each other until
a termination condition fires, and the unit worth recording is the TURN.

The risk worth governing is that a group chat runs away: agents replying to one
another until a round cap, or not terminating at all. A chat that hits its cap
without terminating is a finding, and it is one of the clearest illustrations of
why a budget is not a rate limit — nothing was too fast, and nothing was out of
scope. It simply did not stop.
"""

from typing import Any

from rotascale.client import current_trajectory
from rotascale.middleware._common import logger, truncate


def _speaker(message: Any, sender: Any) -> str:
    if isinstance(message, dict) and message.get("name"):
        return str(message["name"])
    for attr in ("name", "role"):
        value = getattr(sender, attr, None)
        if value:
            return str(value)
    return "unknown"


class _WatchedChat:
    def __init__(self, inner: Any, capture_content: bool, limit: int) -> None:
        self._inner = inner
        self._capture = capture_content
        self._limit = limit
        self._turns = 0

    @property
    def turns(self) -> int:
        return self._turns

    def append(self, message: Any, speaker: Any = None, *args: Any, **kwargs: Any) -> Any:
        self._turns += 1
        trajectory = current_trajectory()
        if trajectory is not None:
            try:
                content = message.get("content") if isinstance(message, dict) else message
                trajectory.plan(
                    framework="autogen",
                    turn=self._turns,
                    speaker=_speaker(message, speaker),
                    content=truncate(str(content), self._limit) if self._capture else None,
                    # subhadipmitra@: Reported per turn rather than only at the
                    # end, so a chat that never terminates is visible WHILE it
                    # is happening rather than after somebody notices the bill.
                    max_round=getattr(self._inner, "max_round", None),
                )
            except Exception:
                logger.warning("rotascale: failed to record a turn", exc_info=True)

        cap = getattr(self._inner, "max_round", None)
        if cap and self._turns >= cap and trajectory is not None:
            try:
                trajectory.plan(
                    framework="autogen",
                    finding="group_chat_hit_round_cap",
                    turns=self._turns,
                    note="the conversation reached its cap without terminating "
                         "on its own; it was stopped by the limit, not by a "
                         "decision",
                )
            except Exception:
                logger.warning("rotascale: failed to record a cap", exc_info=True)

        return self._inner.append(message, speaker, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def watch_autogen(groupchat: Any, *, capture_content: bool = True,
                  content_limit: int = 2000) -> Any:
    """Wrap an AutoGen group chat so every turn lands on the trajectory.

    Records who spoke and when. A chat that reaches `max_round` without
    terminating is recorded as a finding rather than as a normal ending — it
    was stopped by a limit, not by a decision, and those are different.
    """
    return _WatchedChat(groupchat, capture_content, content_limit)
