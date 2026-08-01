"""Rotascale — govern what your agents are allowed to do, and prove what they did."""

from rotascale._version import __version__
from rotascale.client import Agent, Decision, Rotascale, Trajectory, current_trajectory
from rotascale.errors import (
    Blocked,
    EnforcementUnavailable,
    Exhausted,
    Gated,
    ReviewRequired,
    RotascaleError,
)

__all__ = [
    "Agent",
    "Blocked",
    "Decision",
    "EnforcementUnavailable",
    "Exhausted",
    "Gated",
    "ReviewRequired",
    "Rotascale",
    "RotascaleError",
    "Trajectory",
    "__version__",
    "current_trajectory",
]
