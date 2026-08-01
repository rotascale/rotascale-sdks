"""SDK exceptions.

The split that matters: **capture never raises, enforcement always can.**

A recording problem must not break a customer's production agent — evidence is
worth a lot, but not an outage. An authorisation problem must stop the agent,
because an ungoverned action is worse than a delayed one.
"""

from typing import Any


class RotascaleError(Exception):
    """Base for everything the SDK raises."""


class Blocked(RotascaleError):
    """The action was refused: out of scope, past a ceiling, expired, or revoked.

    This must stop the agent. It is not retryable — the answer will be the same
    until a human changes the grant.
    """

    def __init__(self, message: str, decision: Any = None) -> None:
        super().__init__(message)
        self.decision = decision


class Exhausted(Blocked):
    """The grant's budget or call count is spent.

    Separate from Blocked because the remedy is different: someone must raise
    the budget or issue a new grant, rather than change the scope.
    """


class Gated(Blocked):
    """Refused because the trajectory's context is tainted and this grant
    requires a clean one.

    The agent read something untrusted and then tried to act with authority.
    The remedy is a human approval or a declared sanitiser — not a retry.
    """


class ReviewRequired(RotascaleError):
    """A human must decide before the action runs.

    The queue item already exists server-side; park the action and return.
    """

    def __init__(self, message: str, decision: Any = None) -> None:
        super().__init__(message)
        self.decision = decision


class EnforcementUnavailable(RotascaleError):
    """Rotascale could not be reached for an authorisation decision.

    subhadipmitra@: This is deliberately an exception rather than a silent
    allow. Capture fails open; enforcement fails CLOSED. If the control plane is
    unreachable, the honest position is that the action is ungoverned — and an
    ungoverned action is worse than a delayed one. Teams that genuinely cannot
    accept that set `fail_open_enforcement=True` and own the consequence
    explicitly, which is at least a recorded decision rather than an accident.
    """
