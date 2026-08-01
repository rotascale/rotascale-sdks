"""The MCP governance surface.

subhadipmitra@: What is under test is mostly not code — it is the WORDING the
tools return, because the consumer is a language model and the wording is the
interface.

A refusal that says only "not allowed" gets improvised around. An agent told
`exhausted` and given nothing else will retry, which cannot work. An agent told
`gated` and given nothing else may start a fresh trajectory to shed the taint,
which is the exact laundering the control exists to stop. So the guidance text
is asserted as tightly as the outcome itself.
"""

import httpx
from rotascale import Rotascale

from rotascale_mcp.server import build
from rotascale_mcp.session import Session, describe_outcome


def make_session(handler) -> Session:
    client = Rotascale("http://test", api_key="rota_test_x")
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://test")
    return Session(client)


def responder(**overrides):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/agents/resolve":
            return httpx.Response(200, json=overrides.get("agent", {
                "id": "agt_1", "slug": "refund-assistant", "status": "claimed",
                "discovered": False, "governed": True, "notice": None,
            }))
        if path == "/v1/trajectories":
            return httpx.Response(201, json={"id": "trj_1"})
        if path.endswith("/steps"):
            return httpx.Response(201, json={"id": "stp_1", "ordinal": 0})
        if path.endswith("/close"):
            return httpx.Response(200, json={"id": "trj_1", "status": "completed"})
        if path == "/v1/authorize":
            return httpx.Response(200, json=overrides.get("decision", {
                "outcome": "allow", "allowed": True, "reason": "within scope",
                "findings": [], "enforcement_mode": "enforce",
            }))
        if path.endswith("/grants"):
            return httpx.Response(200, json=overrides.get("grants", []))
        return httpx.Response(200, json={})
    return handler


async def call(server, name, **kwargs):
    """Invoke a tool the way a host would, and hand back the structured half.

    The content half is the human-readable rendering; `structured_content` is
    the JSON a model actually reasons over, and it is what these tests assert.
    """
    result = await server.call_tool(name, kwargs)
    assert not result.is_error, result.content
    return result.structured_content


# --- the outcome vocabulary ------------------------------------------------


def test_every_refusal_names_a_remedy_and_forbids_retrying():
    """A model given no remedy improvises, and improvising around a refusal is
    the failure mode this whole surface exists to prevent."""
    for outcome in ("deny", "exhausted", "gated"):
        guidance = describe_outcome(outcome)
        assert "do not proceed" in guidance.lower(), outcome
        assert "retry" in guidance.lower() or "attempt" in guidance.lower(), outcome


def test_gated_forbids_laundering_specifically():
    """The tempting workaround for a tainted context is a fresh trajectory.
    Saying so explicitly is cheaper than detecting it afterwards."""
    guidance = describe_outcome("gated")
    assert "new trajectory" in guidance
    assert "rephras" in guidance


def test_exhausted_says_retrying_cannot_help():
    assert "Retrying will not help" in describe_outcome("exhausted")


def test_an_unknown_outcome_is_treated_as_a_refusal():
    """A server that grows a seventh outcome must not read as permission to a
    client that predates it."""
    assert "refusal" in describe_outcome("some_future_outcome")


# --- the tools -------------------------------------------------------------


async def test_authorize_returns_the_outcome_not_a_boolean():
    server = build(make_session(responder(decision={
        "outcome": "exhausted", "allowed": False,
        "reason": "budget spent", "findings": [],
        "remaining_amount_minor": 0, "enforcement_mode": "enforce",
    })))
    await call(server, "open_trajectory", agent="refund-assistant")
    body = await call(server, "authorize_action", grant_id="grt_1",
                      tools=["issue_refund"], amount_minor=9000)

    assert body["outcome"] == "exhausted"
    assert body["allowed"] is False
    # The distinction a boolean would destroy.
    assert "Retrying will not help" in body["guidance"]


async def test_a_refusal_does_not_raise_across_the_mcp_boundary():
    """An exception becomes an error string a model may well ignore. A
    structured refusal it has been instructed to honour is likelier to land."""
    server = build(make_session(responder(decision={
        "outcome": "deny", "allowed": False, "reason": "out of scope",
        "findings": [], "enforcement_mode": "enforce",
    })))
    await call(server, "open_trajectory", agent="refund-assistant")
    body = await call(server, "authorize_action", grant_id="grt_1")
    assert body["allowed"] is False and body["outcome"] == "deny"


async def test_a_measured_grant_reports_that_it_is_not_enforcing():
    """In observe or shadow the policy refused and the mode let it through. A
    model reporting "approved" would overstate what happened."""
    server = build(make_session(responder(decision={
        "outcome": "allow", "allowed": True, "reason": "observed",
        "findings": ["would_refuse:scope"], "enforcement_mode": "observe",
    })))
    await call(server, "open_trajectory", agent="refund-assistant")
    body = await call(server, "authorize_action", grant_id="grt_1")
    assert body["enforcing"] is False


async def test_opening_says_plainly_when_nothing_is_being_enforced():
    """A discovered agent records evidence and enforces nothing. An integration
    that cannot tell reports success while the customer believes otherwise."""
    server = build(make_session(responder(agent={
        "id": "agt_2", "slug": "brand-new", "status": "discovered",
        "discovered": True, "governed": False,
        "notice": "This agent is recorded but holds no authority.",
    })))
    body = await call(server, "open_trajectory", agent="brand-new")

    assert body["governed"] is False
    assert "no authority" in body["note"]
    assert "refused" in body["note"]


async def test_steps_before_a_trajectory_is_open_fail_loudly():
    """Silently dropping evidence is the one thing capture must not do."""
    server = build(make_session(responder()))
    body = await call(server, "witness_step", kind="tool_call", summary="did a thing")
    assert body["recorded"] is False
    assert "open_trajectory" in body["error"]


async def test_check_authority_reports_what_remains_not_what_was_granted():
    """An agent needs to know it has 200 left, not that it started with 1000."""
    server = build(make_session(responder(grants=[{
        "id": "grt_1", "scope": {"tools": ["issue_refund"]},
        "budget_amount_minor": 100_000, "spent_amount_minor": 99_800,
        "budget_count": 50, "spent_count": 49,
        "not_after": "2026-12-01T00:00:00Z", "status": "active",
        "enforcement_mode": "enforce",
    }])))
    await call(server, "open_trajectory", agent="refund-assistant")
    body = await call(server, "check_authority")

    grant = body["grants"][0]
    assert grant["budget_remaining_minor"] == 200
    assert grant["calls_remaining"] == 1
    assert grant["enforcing"] is True


async def test_a_trajectory_closes_and_a_second_close_is_not_an_error():
    server = build(make_session(responder()))
    await call(server, "open_trajectory", agent="refund-assistant", ref="TICKET-1")
    first = await call(server, "close_trajectory", status="completed")
    second = await call(server, "close_trajectory")

    assert first["closed"] is True and first["trajectory_id"] == "trj_1"
    assert second["closed"] is False


# --- honesty about what this surface is ------------------------------------


def test_the_readme_does_not_call_the_opt_in_surface_a_control():
    """The one claim we must not make. An agent that never calls
    `authorize_action` is not governed by it, and a customer who believes
    otherwise has bought a control they do not have."""
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "Only the proxy is a control" in readme
    assert "Can the agent avoid it" in readme


async def test_check_authority_survives_a_refusal_and_says_so():
    """An older server refuses an API key here, and a refusal is a problem
    document rather than a list. Iterating it used to raise a TypeError that
    reached the host as an opaque tool failure — which tells a model nothing,
    and a model told nothing about its own authority will guess."""
    def refusing(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/grants"):
            return httpx.Response(403, json={"title": "Forbidden"})
        return responder()(request)

    server = build(make_session(refusing))
    await call(server, "open_trajectory", agent="refund-assistant")
    body = await call(server, "check_authority")

    assert "403" in body["error"]
    # Never let a failed read read as "no restrictions".
    assert "Do not assume you hold any authority" in body["guidance"]
