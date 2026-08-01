"""Shared helpers for provider middlewares."""

import logging
from typing import Any

from rotascale.client import current_trajectory

logger = logging.getLogger("rotascale.middleware")


def record(kind: str, /, **payload: Any) -> None:
    """Attach a step to the trajectory in scope. No trajectory means no-op —
    evidence belongs to a governed unit of work, not to stray calls."""
    trajectory = current_trajectory()
    if trajectory is None:
        return
    try:
        getattr(trajectory, kind, trajectory.step)(**payload) if hasattr(
            trajectory, kind
        ) else trajectory.step(kind, **payload)
    except Exception:
        logger.warning("rotascale: failed to record %s", kind, exc_info=True)


def truncate(value: Any, limit: int) -> Any:
    """Cap captured content.

    subhadipmitra@: Prompts and completions can be enormous, and the evidence
    store is not a log sink. Truncation is visible (the marker is kept) so
    nobody mistakes a clipped record for the whole story.
    """
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…[truncated {len(value) - limit} chars]"
    return value


# subhadipmitra@: Which agents have already reported which model, this process.
#
# Provenance is deduplicated server-side on a content hash, so re-reporting is
# harmless — but it is an HTTP call on the agent's critical path, and doing it
# per model call rather than per model CHANGE would put a round trip in front of
# every completion. Keyed by (agent, model) so a genuine switch still reports.
_reported: set[tuple[str, str]] = set()


def report_served_model(model: str | None, provider: str | None = None) -> None:
    """Tell Rotascale which model actually served the call.

    The SERVED identity, not the requested one. They differ — ask for
    `gpt-4o` and a dated build answers, ask Ollama for a tag and it names the
    digest it loaded — and only the served one is evidence of what ran.

    Never raises. A provider middleware that broke an agent because the
    inventory was unreachable would be indefensible.
    """
    if not model:
        return
    trajectory = current_trajectory()
    if trajectory is None or not getattr(trajectory, "agent_id", None):
        return

    key = (trajectory.agent_id, model)
    if key in _reported:
        return
    _reported.add(key)

    try:
        trajectory._client.report_provenance(
            trajectory.agent_id, model=model, provider=provider)
    except Exception:
        logger.warning("rotascale: could not report the served model", exc_info=True)
