"""Rotascale — govern what your agents are allowed to do, and prove what they did."""

from rotascale.client import Agent, Decision, Rotascale, Trajectory, current_trajectory
from rotascale.errors import (
    Blocked,
    EnforcementUnavailable,
    Exhausted,
    Gated,
    ReviewRequired,
    RotascaleError,
)

__version__ = "0.1.0"
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
    "current_trajectory",
]
