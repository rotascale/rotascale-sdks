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
