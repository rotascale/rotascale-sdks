"""Annotate OpenTelemetry spans so Rotascale can govern them (`#204`).

subhadipmitra@: This is for people who ALREADY emit OTel and do not want a
second reporting path. It is not the SDK's normal route.

The normal route is `Rotascale(...).agent(...).trajectory(...)`, which posts
directly and is what most callers want. If you are already tracing every tool
call and model call, that is a second copy of the same events, and two capture
paths that can disagree is worse than either alone.

So: keep your spans, add these attributes, and point your collector at
`https://<your-deployment>/v1/otel/traces`. The spans become trajectories and
steps, with taint propagated and authority context intact.

    from rotascale.otel import govern, TOOL_CALL

    with tracer.start_as_current_span("invoice_lookup") as span:
        govern(span, agent="refund-bot", kind=TOOL_CALL, source_ref="invoices-api")
        ...

Or set the agent once, on the resource, where an exporter usually sets service
attributes:

    Resource.create({"rotascale.agent": "refund-bot", **})

## No opentelemetry dependency, on purpose

Nothing here imports `opentelemetry`. `govern()` takes anything with a
`set_attribute` method, which every OTel span has and which a test double has in
three lines. The base package is installed overwhelmingly by agents that will
never emit a span, and making them carry an SDK for a function that sets six
strings would be the wrong trade.

## These names are a PROPOSAL

`rotascale.*` is a holding prefix. `agent-governance-spec` deliberately does not
declare this mapping — its position is that it belongs upstream at
OpenTelemetry rather than in a vendor's document. If it is accepted there the
attributes change, and this module is the one place a caller has to follow.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# --- the convention, mirroring the receiver -------------------------------

AGENT = "rotascale.agent"
STEP_KIND = "rotascale.step.kind"
GRANT = "rotascale.grant"
SOURCE_REF = "rotascale.source_ref"
TRUSTED = "rotascale.trusted"
DISCHARGES = "rotascale.discharges"
GOAL = "rotascale.goal"

# --- step kinds a caller will actually reach for ---------------------------

PLAN = "plan"
LLM_CALL = "llm_call"
TOOL_CALL = "tool_call"
RETRIEVAL = "retrieval"
DELEGATION = "delegation"
SANITISE = "sanitise"
HUMAN_REVIEW = "human_review"
DISCLOSURE = "disclosure"
ENFORCEMENT = "enforcement"

#: The kinds that introduce taint, which is the reason to be deliberate about
#: `kind` rather than letting the receiver guess. The receiver infers `llm_call`
#: freely and never infers these: they gate authority, and a control that
#: refuses real work because something was guessed is a control people uninstall.
TAINTING = frozenset({TOOL_CALL, RETRIEVAL, DELEGATION})


@runtime_checkable
class Span(Protocol):
    def set_attribute(self, key: str, value: Any) -> Any: ...


def govern(
    span: Span,
    *,
    agent: str | None = None,
    kind: str | None = None,
    grant: str | None = None,
    source_ref: str | None = None,
    trusted: bool | None = None,
    discharges: list[str] | str | None = None,
    goal: str | None = None,
) -> Span:
    """Set the governance attributes on a span. Returns the span, for chaining.

    Every argument is optional and `None` sets nothing, so this can be called
    twice on the same span without clearing what the first call set.

    `source_ref` matters more than it looks for a tainting `kind`: it becomes
    the label an operator reads when a gate refuses, and a taint label naming
    nothing tells them something untrusted happened and not what.
    """
    if agent is not None:
        span.set_attribute(AGENT, agent)
    if kind is not None:
        span.set_attribute(STEP_KIND, kind)
    if grant is not None:
        span.set_attribute(GRANT, grant)
    if source_ref is not None:
        span.set_attribute(SOURCE_REF, source_ref)
    if trusted is not None:
        span.set_attribute(TRUSTED, bool(trusted))
    if discharges is not None:
        value = discharges if isinstance(discharges, str) else ",".join(discharges)
        span.set_attribute(DISCHARGES, value)
    if goal is not None:
        span.set_attribute(GOAL, goal)
    return span


def resource_attributes(agent: str) -> dict[str, str]:
    """Attributes to put on an OTel Resource, so every span carries the agent.

    A span that names a different agent overrides this, because being specific
    on one span is deliberate.
    """
    return {AGENT: agent}
