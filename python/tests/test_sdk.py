"""SDK behaviour.

The contract under test throughout: **capture never raises, enforcement always
can.** Losing evidence is survivable; losing the authority check is not.
"""

import httpx
import pytest

from rotascale import Blocked, EnforcementUnavailable, Exhausted, Gated, ReviewRequired, Rotascale
from rotascale.client import current_trajectory


def make_client(handler, **kw) -> Rotascale:
    client = Rotascale("http://test", token="t", **kw)
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="http://test",
        headers={"authorization": "Bearer t"},
    )
    return client


def ok_handler(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/trajectories":
            return httpx.Response(201, json={"id": "trj_1"})
        if request.url.path.endswith("/steps"):
            return httpx.Response(201, json={"id": "stp_1", "ordinal": 0})
        if request.url.path.endswith("/close"):
            return httpx.Response(200, json={"id": "trj_1", "status": "completed"})
        if request.url.path == "/v1/authorize":
            return httpx.Response(200, json={
                "outcome": "allow", "allowed": True, "reason": "authorised",
                "grant_id": "grt_1", "ledger_id": "led_1",
                "remaining_amount_minor": 1000, "remaining_count": None, "findings": [],
            })
        return httpx.Response(404)
    return handler


class TestHappyPath:
    def test_witness_opens_records_and_closes(self):
        calls: list[httpx.Request] = []
        rs = make_client(ok_handler(calls))
        with rs.witness("agt_1", ref="TICKET-1", goal={"task": "refund"}) as t:
            t.plan(strategy="check policy then refund")
            t.retrieval("https://untrusted.example/note.pdf")
            t.llm_call(model="gpt-4o")
            t.outcome(decision="approved")

        paths = [c.url.path for c in calls]
        assert paths[0] == "/v1/trajectories"
        assert paths.count("/v1/trajectories/trj_1/steps") == 3
        assert paths[-1] == "/v1/trajectories/trj_1/close"

    def test_trajectory_is_available_in_context(self):
        rs = make_client(ok_handler([]))
        assert current_trajectory() is None
        with rs.witness("agt_1") as t:
            assert current_trajectory() is t
        assert current_trajectory() is None, "context must not leak past the block"

    def test_external_ref_is_forwarded_for_idempotency(self):
        calls: list[httpx.Request] = []
        rs = make_client(ok_handler(calls))
        with rs.witness("agt_1", ref="TICKET-42"):
            pass
        import json
        assert json.loads(calls[0].content)["external_ref"] == "TICKET-42"


class TestCaptureFailsOpen:
    """A recording problem must never break the host agent."""

    def test_a_dead_control_plane_does_not_break_the_agent(self):
        def dead(request):
            raise httpx.ConnectError("connection refused")

        rs = make_client(dead)
        did_work = False
        with rs.witness("agt_1") as t:      # must not raise
            t.plan(step="one")
            t.retrieval("https://x.example")
            did_work = True
        assert did_work, "the agent must run even with Rotascale down"

    def test_step_errors_are_swallowed(self):
        def flaky(request):
            if request.url.path == "/v1/trajectories":
                return httpx.Response(201, json={"id": "trj_1"})
            return httpx.Response(500, json={"detail": "boom"})

        rs = make_client(flaky)
        with rs.witness("agt_1") as t:
            t.plan(x=1)                      # 500 on the step, no exception

    def test_an_agent_exception_closes_the_trajectory_as_failed(self):
        """A crash is evidence too — the record must say the agent died, not
        trail off silently."""
        calls: list[httpx.Request] = []
        rs = make_client(ok_handler(calls))
        with pytest.raises(ValueError, match="agent blew up"), rs.witness("agt_1") as t:
            t.plan(x=1)
            raise ValueError("agent blew up")

        import json
        closing = json.loads(calls[-1].content)
        assert closing["status"] == "failed"
        assert closing["outcome"]["error"] == "ValueError"
        assert "agent blew up" in closing["outcome"]["message"]

    def test_recording_after_close_is_a_no_op(self):
        calls: list[httpx.Request] = []
        rs = make_client(ok_handler(calls))
        with rs.witness("agt_1") as t:
            pass
        t.plan(late=True)                    # must not raise, must not send
        assert not any(c.url.path.endswith("/steps") for c in calls)


class TestEnforcementFailsClosed:
    """An ungoverned action is worse than a delayed one."""

    def test_unreachable_control_plane_raises(self):
        def dead(request):
            raise httpx.ConnectError("connection refused")

        rs = make_client(dead)
        with pytest.raises(EnforcementUnavailable):
            rs.authorize("grt_1", {"tools": ["issue_refund"]})

    def test_fail_open_is_possible_but_must_be_chosen_explicitly(self):
        def dead(request):
            raise httpx.ConnectError("connection refused")

        rs = make_client(dead, fail_open_enforcement=True)
        decision = rs.authorize("grt_1", {"tools": ["issue_refund"]})
        assert decision.allowed
        assert "failing open" in decision.reason

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            ("deny", Blocked),
            ("exhausted", Exhausted),
            ("gated", Gated),
            ("review_sync", ReviewRequired),
        ],
    )
    def test_each_refusal_raises_the_type_that_names_its_remedy(self, outcome, expected):
        """Different refusals need different fixes: change the scope, raise the
        budget, get an approval. The exception type says which."""
        def handler(request):
            return httpx.Response(200, json={
                "outcome": outcome, "allowed": False, "reason": f"refused: {outcome}",
                "grant_id": "grt_1", "ledger_id": "led_1", "findings": [],
            })

        rs = make_client(handler)
        with pytest.raises(expected) as exc:
            rs.authorize("grt_1", {"tools": ["x"]})
        assert exc.value.decision.outcome == outcome

    def test_refusals_can_be_handled_without_exceptions(self):
        def handler(request):
            return httpx.Response(200, json={
                "outcome": "deny", "allowed": False, "reason": "out of scope",
                "grant_id": "grt_1", "ledger_id": None, "findings": [],
            })

        rs = make_client(handler)
        decision = rs.authorize("grt_1", {"tools": ["x"]}, raise_on_refusal=False)
        assert not decision.allowed and decision.outcome == "deny"

    def test_async_review_is_allowed_to_proceed(self):
        def handler(request):
            return httpx.Response(200, json={
                "outcome": "review_async", "allowed": True,
                "reason": "reviewed after the fact", "grant_id": "grt_1",
                "ledger_id": "led_1", "findings": [],
            })

        rs = make_client(handler)
        decision = rs.authorize("grt_1", {"tools": ["x"]})
        assert decision.allowed and decision.needs_review

    def test_authorize_inside_witness_carries_the_trajectory(self):
        calls: list[httpx.Request] = []
        rs = make_client(ok_handler(calls))
        with rs.witness("agt_1") as t:
            t.authorize("grt_1", {"tools": ["issue_refund"]}, amount_minor=500)

        import json
        body = json.loads(next(c for c in calls if c.url.path == "/v1/authorize").content)
        assert body["trajectory_id"] == "trj_1"
        assert body["amount_minor"] == 500

    def test_enforcement_still_works_when_capture_is_dead(self):
        """The most important combination: evidence is down, authority is not.
        The agent must still be governed."""
        def handler(request):
            if request.url.path == "/v1/trajectories":
                raise httpx.ConnectError("evidence store down")
            if request.url.path == "/v1/authorize":
                return httpx.Response(200, json={
                    "outcome": "deny", "allowed": False, "reason": "out of scope",
                    "grant_id": "grt_1", "ledger_id": None, "findings": [],
                })
            return httpx.Response(500)

        rs = make_client(handler)
        # Capture degrades to a null trajectory; enforcement still fails closed.
        with rs.witness("agt_1") as t, pytest.raises(Blocked):
            t.authorize("grt_1", {"tools": ["forbidden"]})


# --- enforcement visibility ------------------------------------------------
#
# subhadipmitra@: A caller must be able to tell that the control they rely on is
# measuring rather than enforcing. Without this the SDK reports `allow` for a
# grant in observe and looks exactly like a grant that is refusing things —
# which is the same failure fixed twice server-side, on the surface customers
# actually integrate against.


def test_a_non_enforcing_grant_is_visible_to_the_caller():
    from rotascale.client import Decision

    observing = Decision(
        outcome="allow", allowed=True, reason="not enforced (observe)",
        grant_id="grt_1", policy_outcome="deny", enforcement_mode="observe",
        findings=["would_refuse:deny:out of scope"],
    )
    assert observing.allowed is True
    assert observing.enforcing is False
    assert observing.suppressed is True


def test_an_enforcing_grant_reports_itself_as_such():
    from rotascale.client import Decision

    enforcing = Decision(
        outcome="deny", allowed=False, reason="out of scope",
        grant_id="grt_2", policy_outcome="deny", enforcement_mode="enforce",
    )
    assert enforcing.enforcing is True
    assert enforcing.suppressed is False


def test_an_older_server_is_assumed_to_be_enforcing():
    """Assuming a control is off when it is on is the safer error to make."""
    from rotascale.client import Decision

    legacy = Decision(outcome="allow", allowed=True, reason="authorised",
                      grant_id="grt_3")
    assert legacy.enforcing is True
    assert legacy.suppressed is False


def test_suppression_is_detectable_without_policy_outcome():
    """Falls back to the findings, so a partly-upgraded server still tells the
    truth rather than silently reporting enforcement."""
    from rotascale.client import Decision

    legacy = Decision(
        outcome="allow", allowed=True, reason="not enforced (observe)",
        grant_id="grt_4", findings=["would_refuse:deny:out of scope"],
    )
    assert legacy.suppressed is True


def test_the_non_enforcing_warning_fires_once_per_grant(caplog):
    import logging

    from rotascale.client import _ANNOUNCED, Decision, _warn_if_not_enforcing

    _ANNOUNCED.clear()
    decision = Decision(outcome="allow", allowed=True, reason="not enforced (observe)",
                        grant_id="grt_noisy", enforcement_mode="observe")
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            _warn_if_not_enforcing(decision)
    warnings = [r for r in caplog.records if "NOT refusing anything" in r.message]
    assert len(warnings) == 1, "a per-call warning gets filtered out and protects nobody"


# --- credentials -----------------------------------------------------------
#
# subhadipmitra@: An agent inside a customer runtime cannot complete an OIDC
# flow, so the API key is the path that actually gets used. These pin the two
# things that made the old behaviour hostile: constructing without credentials
# and failing much later, and a typo'd key producing a bare 401.


def test_an_api_key_authenticates():
    client = Rotascale("http://test", api_key="rota_live_abc123")
    assert client.http.headers["authorization"] == "Bearer rota_live_abc123"


def test_the_api_key_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("ROTASCALE_API_KEY", "rota_test_fromenv")
    client = Rotascale("http://test")
    assert client.http.headers["authorization"] == "Bearer rota_test_fromenv"


def test_an_oidc_token_still_works_for_a_person():
    """A human driving the SDK against their own console session."""
    client = Rotascale("http://test", token="an-oidc-token")
    assert client.http.headers["authorization"] == "Bearer an-oidc-token"


def test_the_key_wins_when_both_are_supplied():
    """It is the credential an agent is meant to use."""
    client = Rotascale("http://test", api_key="rota_live_key", token="tok")
    assert client.http.headers["authorization"] == "Bearer rota_live_key"


def test_constructing_without_credentials_fails_immediately(monkeypatch):
    """Not on the first call, when the agent is already running.

    The old behaviour built happily and then produced a bare 401 from the first
    authorisation — a stack trace instead of a sentence, at the worst moment.
    """
    monkeypatch.delenv("ROTASCALE_API_KEY", raising=False)
    monkeypatch.delenv("ROTASCALE_TOKEN", raising=False)

    with pytest.raises(ValueError) as exc:
        Rotascale("http://test")

    message = str(exc.value)
    assert "ROTASCALE_API_KEY" in message
    # Says where to get one and what it names. "Missing credentials" would be
    # accurate and useless.
    assert "console" in message
    assert "workspace rather than an agent" in message


def test_a_key_that_is_not_a_key_is_rejected_locally(monkeypatch):
    """The server deliberately says only 'api key rejected' and cannot tell
    anyone their key looked malformed. So the client says it."""
    monkeypatch.delenv("ROTASCALE_API_KEY", raising=False)

    with pytest.raises(ValueError) as exc:
        Rotascale("http://test", api_key="sk-an-openai-key-by-mistake")
    assert "does not look like a Rotascale key" in str(exc.value)


def test_the_readme_zero_argument_form_works(monkeypatch):
    """`Rotascale()` with both env vars set — what the README leads with.

    Pinned because the README is the first thing anyone runs, and a headline
    example that raises is worse than no example.
    """
    monkeypatch.setenv("ROTASCALE_URL", "https://rotascale.acme.internal/")
    monkeypatch.setenv("ROTASCALE_API_KEY", "rota_live_readme")

    client = Rotascale()

    assert client.base_url == "https://rotascale.acme.internal"   # trailing / trimmed
    assert client.http.headers["authorization"] == "Bearer rota_live_readme"
