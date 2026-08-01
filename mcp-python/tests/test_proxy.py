"""The proxy — the surface an agent cannot opt out of.

subhadipmitra@: The one property that must hold is that a refused call **never
reaches the downstream tool**. Everything else here is about not breaking the
relay while enforcing that.

The second property, almost as important: a governance failure must not wedge
the customer's agent. A proxy that hangs because Rotascale is slow has caused
the outage it was sold to prevent.
"""

import httpx
import pytest
from rotascale import Agent, Rotascale

from rotascale_mcp.proxy import Governor, _blocked, _parse, _tools_from_result


def make_client(handler) -> Rotascale:
    client = Rotascale("http://test", api_key="rota_test_x")
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://test")
    return client


def responder(*, decision=None, grants=None, observe=None):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/trajectories":
            return httpx.Response(201, json={"id": "trj_1"})
        if path.endswith("/steps"):
            return httpx.Response(201, json={"id": "stp_1", "ordinal": 0})
        if path.endswith("/grants"):
            return httpx.Response(200, json=grants if grants is not None else [])
        if path == "/v1/authorize":
            return httpx.Response(200, json=decision or {
                "outcome": "allow", "allowed": True, "reason": "within scope",
                "findings": [], "enforcement_mode": "enforce"})
        if path == "/v1/mcp/observe":
            return httpx.Response(200, json=observe or {
                "server_id": "mcs_1", "discovered": True, "tool_count": 1,
                "changes": [], "injection_risk": False})
        return httpx.Response(200, json={})
    return handler


AGENT = Agent(id="agt_1", slug="refund-assistant", status="claimed", governed=True)

GRANT = [{"id": "grt_1", "scope": {"tools": ["send_email"]},
          "status": "active", "enforcement_mode": "enforce"}]


async def governor(**kw) -> Governor:
    g = Governor(make_client(responder(**kw)), AGENT, server_name="mailer")
    await g.start(ref=None)
    return g


# --- the property that matters ---------------------------------------------


async def test_a_refused_call_is_stopped_before_the_downstream_tool():
    g = await governor(grants=GRANT, decision={
        "outcome": "deny", "allowed": False, "reason": "outside scope",
        "findings": [], "enforcement_mode": "enforce"})

    reply = await g.decide(7, "send_email", {"to": "x@y.z"})

    assert reply is not None, "a refusal must not be forwarded"
    assert reply["id"] == 7
    assert reply["result"]["isError"] is True
    assert reply["result"]["_rotascale"]["blocked"] is True


async def test_an_allowed_call_is_forwarded_untouched():
    g = await governor(grants=GRANT)
    assert await g.decide(1, "send_email", {"to": "x@y.z"}) is None


async def test_a_block_is_a_tool_result_not_a_protocol_error():
    """subhadipmitra@: A JSON-RPC error is often swallowed by the host and shown
    to the model as "the tool failed", which invites a retry. A result puts our
    sentence in front of the model where it can be read and obeyed."""
    reply = _blocked(3, "gated", "context is tainted")

    assert "error" not in reply
    assert reply["result"]["isError"] is True
    text = reply["result"]["content"][0]["text"]
    assert "BLOCKED by Rotascale" in text
    # The remedy travels with the refusal.
    assert "new trajectory" in text


async def test_each_outcome_carries_its_own_remedy():
    for outcome, expected in (
        ("exhausted", "Retrying will not help"),
        ("deny", "do not attempt a different route"),
        ("review_sync", "PAUSE"),
    ):
        text = _blocked(1, outcome, "because")["result"]["content"][0]["text"]
        assert expected in text, outcome


# --- scope matching ---------------------------------------------------------


async def test_scope_matching_is_exact_not_wildcard():
    """A scope states what a human authorised. Inferring that `file_*` covers
    `file_delete` puts words in their mouth."""
    g = await governor(grants=[{"id": "grt_1", "scope": {"tools": ["file_read"]},
                                "status": "active"}])
    assert g.grant_for("file_read") == "grt_1"
    assert g.grant_for("file_delete") is None
    assert g.grant_for("file_") is None


async def test_a_tool_no_grant_covers_is_recorded_and_forwarded_by_default():
    """Refusing everything unconfigured turns a fresh install into a broken one.
    The call is recorded, and the console shows it as a finding."""
    g = await governor(grants=[])
    assert await g.decide(1, "anything", {}) is None


async def test_require_grant_makes_it_fail_closed(monkeypatch):
    import rotascale_mcp.proxy as proxy
    monkeypatch.setattr(proxy, "REQUIRE_GRANT", True)

    g = await governor(grants=[])
    reply = await g.decide(1, "anything", {})

    assert reply is not None
    assert "requires one" in reply["result"]["_rotascale"]["reason"]


# --- manifests --------------------------------------------------------------


async def test_a_poisoned_manifest_taints_the_trajectory():
    """Taint is what makes the NEXT privileged action refuse. Noting the change
    without tainting would leave the injection detected but not stopped."""
    recorded: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/steps"):
            import json
            recorded.append(json.loads(request.content))
            return httpx.Response(201, json={"id": "stp_1", "ordinal": 0})
        return responder(observe={
            "server_id": "mcs_1", "discovered": False, "tool_count": 1,
            "changes": [{"kind": "description_changed"}],
            "injection_risk": True})(request)

    g = Governor(make_client(handler), AGENT, server_name="mailer")
    await g.start(ref=None)
    await g.observe_manifest(
        [{"name": "send_email", "description": "now evil", "inputSchema": {}}])

    taints = [s for s in recorded if s.get("kind") == "retrieval"]
    assert taints, "a poisoned manifest must taint the trajectory"
    assert taints[0]["trusted_source"] is False


async def test_argument_names_are_sent_but_never_their_values():
    """A tool call's arguments are the customer's data. Which fields were
    supplied is governance-relevant; what was in them is not ours to take."""
    sent: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/authorize":
            import json
            sent.append(json.loads(request.content))
        return responder(grants=GRANT)(request)

    g = Governor(make_client(handler), AGENT, server_name="mailer")
    await g.start(ref=None)
    await g.decide(1, "send_email",
                   {"to": "victim@example.com", "body": "secret material"})

    blob = str(sent)
    assert "to" in blob and "body" in blob
    assert "victim@example.com" not in blob
    assert "secret material" not in blob


# --- the relay must not wedge ----------------------------------------------


async def test_a_malformed_line_is_not_treated_as_a_message():
    assert _parse(b"not json\n") is None
    assert _parse(b'"a string"\n') is None
    assert _parse(b'{"jsonrpc":"2.0"}\n') == {"jsonrpc": "2.0"}


async def test_a_result_without_tools_is_ignored():
    assert _tools_from_result({"result": {}}) == []
    assert _tools_from_result({"result": {"tools": "nonsense"}}) == []
    assert _tools_from_result({"result": {"tools": [{"name": "a"}]}}) == [{"name": "a"}]


async def test_an_unreachable_control_plane_does_not_stop_the_agent():
    """Capture fails open. Enforcement is the only thing allowed to block, and
    only when it actually returned a refusal."""
    def dead(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/grants"):
            return httpx.Response(200, json=[])
        if request.url.path == "/v1/trajectories":
            return httpx.Response(201, json={"id": "trj_1"})
        raise httpx.ConnectError("control plane down")

    g = Governor(make_client(dead), AGENT, server_name="mailer")
    await g.start(ref=None)
    # No grant covers it, the step record fails, and the call still goes on.
    assert await g.decide(1, "send_email", {}) is None


# --- honesty ----------------------------------------------------------------


def test_the_readme_documents_the_proxy_as_the_control():
    from pathlib import Path
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
    assert "rotascale-mcp-proxy" in readme
    assert "Only the proxy is a control" in readme


@pytest.mark.parametrize("outcome", ["deny", "exhausted", "gated", "review_sync"])
def test_no_refusal_reads_as_permission(outcome):
    text = _blocked(1, outcome, "r")["result"]["content"][0]["text"]
    assert "BLOCKED" in text
    assert text.lower().count("proceed.") <= 1 or "do not proceed" in text.lower()
