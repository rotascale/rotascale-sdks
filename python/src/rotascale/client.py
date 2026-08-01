"""Rotascale client.

The whole happy path:

    from rotascale import Rotascale

    rs = Rotascale("https://rotascale.acme.internal", token=TOKEN)

    with rs.witness("refund-agent", ref="TICKET-88123") as t:
        t.retrieval("https://customer-attachment.example/note.pdf")   # taints
        d = t.authorize(grant, {"tools": ["issue_refund"]}, amount_minor=9_000)
        if d.allowed:
            issue_refund(...)
        t.outcome(decision="approved" if d.allowed else "blocked")

Three lines to record, one to enforce. Anything demanding an agent rewrite or a
framework migration is rejected at design time.
"""

import contextvars
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from rotascale._version import USER_AGENT
from rotascale.errors import (
    Blocked,
    EnforcementUnavailable,
    Exhausted,
    Gated,
    ReviewRequired,
)

logger = logging.getLogger("rotascale")


# The trajectory currently in scope. Middlewares attach steps to it without the
# caller threading a handle through every function.
_current: contextvars.ContextVar["Trajectory | None"] = contextvars.ContextVar(
    "rotascale_current_trajectory", default=None
)


def current_trajectory() -> "Trajectory | None":
    return _current.get()


@dataclass(frozen=True)
class Decision:
    outcome: str
    allowed: bool
    reason: str
    grant_id: str | None = None
    ledger_id: str | None = None
    remaining_amount_minor: int | None = None
    remaining_count: int | None = None
    findings: list[str] = field(default_factory=list)
    # What the policy decided, before the grant's enforcement mode was applied,
    # and the mode itself. Both may be None against an older server.
    policy_outcome: str | None = None
    enforcement_mode: str | None = None

    @property
    def needs_review(self) -> bool:
        return self.outcome in ("review_sync", "review_async")

    @property
    def enforcing(self) -> bool:
        """Is this grant actually refusing anything?

        subhadipmitra@: A grant in observe returns `allow` for everything. Ask
        this rather than comparing `enforcement_mode` to a string — the set of
        modes will grow, and a caller who wrote `mode == "enforce"` would
        silently start treating a new non-enforcing mode as enforcement.

        Unknown (an older server) is reported as enforcing, because assuming a
        control is off when it is on is the safer error for a caller to make.
        """
        return self.enforcement_mode in (None, "enforce")

    @property
    def suppressed(self) -> bool:
        """The policy refused this and the enforcement mode let it through.

        True only while a grant is being measured. If this is ever true in
        production, the control you believe is running is not.
        """
        if self.policy_outcome is None:
            return any(f.startswith("would_refuse:") for f in self.findings)
        return self.policy_outcome != self.outcome


#: Spellings that mean `ref` and are not it. Named explicitly rather than fuzzy-
#: matched: `ref` is three characters, so difflib rates "reference" at 0.5
#: against it and a cutoff loose enough to catch that would flag half the
#: forward-compatible fields this parameter exists to allow.
_MEANT_REF = {
    "reference", "refernce", "refrence", "ref_id", "refid", "refs",
    "external_reference", "idempotency_key", "idempotency", "trace_id",
    "correlation_id", "request_id",
}


def _reject_near_misses(supplied: dict[str, Any], call: str) -> None:
    """Refuse a keyword that plainly means `ref` and is not it.

    subhadipmitra@: `**kw` exists so a customer can pass a field a newer server
    understands without waiting for an SDK release, and that is worth keeping.
    The cost is that a typo is silent — `witness(agent, reference="TICKET-1")`
    sends an unknown field, the server ignores it, and `ref` stays None.

    That matters more here than in an ordinary client, because `ref` is the
    IDEMPOTENCY KEY. Silently dropped, a retry forks history instead of
    continuing it, and nobody finds out until they are reading a trajectory
    list wondering why one action appears three times.

    Only the known confusions raise. Anything genuinely unfamiliar still passes
    through, so forward-compatibility survives and the typo does not.
    """
    for name in supplied:
        if name.lower() in _MEANT_REF:
            raise TypeError(
                f"{call}() got {name!r}. The parameter is `ref`, and unknown "
                f"keywords are forwarded to the server on purpose — so this "
                f"would have been silently ignored. `ref` is the idempotency "
                f"key: without it a retry forks history instead of continuing "
                f"it."
            )


@dataclass(frozen=True)
class Agent:
    """An agent as Rotascale knows it, returned by `Rotascale.agent(slug)`.

    subhadipmitra@: `governed` is the field worth checking. An agent that has
    just been discovered records evidence perfectly well but holds no
    authority, and an integration that cannot tell the difference will report
    success while the customer believes something is being enforced.
    """

    id: str
    slug: str
    status: str
    governed: bool

    def __str__(self) -> str:
        return self.slug


class Trajectory:
    """A governed unit of agent work.

    Every recording method is best-effort. If Rotascale is unreachable, the
    agent keeps working and the SDK logs a warning — losing evidence is bad,
    taking down production is worse.
    """

    def __init__(self, client: "Rotascale", trajectory_id: str, agent_id: str) -> None:
        self.id = trajectory_id
        self.agent_id = agent_id
        self._client = client
        self._closed = False
        self._token: contextvars.Token | None = None

    # --- recording: never raises ------------------------------------------

    def step(self, kind: str, /, **payload: Any) -> None:
        self._record(kind, payload=payload)

    def plan(self, **payload: Any) -> None:
        self._record("plan", payload=payload)

    def llm_call(self, **payload: Any) -> None:
        """A model call. Introduces no taint: the model is not an untrusted
        source, its inputs are."""
        self._record("llm_call", payload=payload)

    def tool_call(self, tool: str, /, *, trusted: bool = False, **payload: Any) -> None:
        self._record("tool_call", payload={"tool": tool, **payload},
                     source_ref=tool, trusted_source=trusted)

    def retrieval(self, source: str, /, *, trusted: bool = False, **payload: Any) -> None:
        """Reading a document, page, or knowledge source.

        Taints the trajectory unless the source is attested trusted — which is
        itself a governance claim, recorded as such.
        """
        self._record("retrieval", payload=payload, source_ref=source, trusted_source=trusted)

    def delegation(self, agent: str, /, **payload: Any) -> None:
        self._record("delegation", payload=payload, source_ref=agent)

    def sanitise(self, *discharges: str, **payload: Any) -> None:
        """Declare that a sanitiser cleared specific taint kinds."""
        self._record("sanitise", payload=payload, discharges=list(discharges))

    def human_review(self, **payload: Any) -> None:
        self._record("human_review", payload=payload)

    def disclosure(self, **payload: Any) -> None:
        """Record that AI involvement was disclosed to the affected person."""
        self._record("disclosure", payload=payload)

    def _record(self, kind: str, **body: Any) -> None:
        if self._closed:
            logger.warning("rotascale: ignoring %s step on a closed trajectory", kind)
            return
        try:
            self._client._post(f"/v1/trajectories/{self.id}/steps", {"kind": kind, **body})
        except Exception:
            logger.warning("rotascale: failed to record %s step", kind, exc_info=True)

    # --- enforcement: raises by design ------------------------------------

    def authorize(
        self,
        grant_id: str,
        scope: dict[str, list[str]] | None = None,
        /,
        *,
        amount_minor: int = 0,
        currency: str | None = None,
        stakes_minor: int | None = None,
        raise_on_refusal: bool = True,
        **action: Any,
    ) -> Decision:
        """Ask whether the agent may act, and consume budget if so.

        Raises by default, because the common mistake is checking `.allowed` and
        forgetting the branch. Pass `raise_on_refusal=False` to handle outcomes
        yourself.
        """
        return self._client.authorize(
            grant_id,
            scope,
            amount_minor=amount_minor,
            currency=currency,
            stakes_minor=stakes_minor,
            trajectory_id=self.id,
            raise_on_refusal=raise_on_refusal,
            **action,
        )

    # --- closing ----------------------------------------------------------

    def outcome(self, **outcome: Any) -> None:
        """Set the outcome. The trajectory closes on context exit."""
        self._outcome = outcome

    def close(self, status: str = "completed", **outcome: Any) -> None:
        if self._closed:
            return
        merged = {**getattr(self, "_outcome", {}), **outcome}
        try:
            self._client._post(
                f"/v1/trajectories/{self.id}/close", {"outcome": merged, "status": status}
            )
        except Exception:
            logger.warning("rotascale: failed to close trajectory %s", self.id, exc_info=True)
        finally:
            self._closed = True


class Rotascale:
    """Synchronous client. Thread-safe; one instance per process is plenty."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        token: str | None = None,
        timeout: float = 5.0,
        enforcement_timeout: float = 10.0,
        fail_open_enforcement: bool = False,
        workspace: str | None = None,
    ) -> None:
        """
        Args:
            api_key: A workspace key, `rota_live_…` or `rota_test_…`. This is
                how an agent authenticates. Falls back to `ROTASCALE_API_KEY`.
            token: An OIDC bearer token. For a person driving the SDK against
                their own console session — an agent inside a customer runtime
                has no practical way to obtain one.
        """
        base_url = base_url or os.environ.get("ROTASCALE_URL")
        if not base_url:
            raise ValueError("base_url is required (or set ROTASCALE_URL)")
        self.base_url = base_url.rstrip("/")

        self._api_key = api_key or os.environ.get("ROTASCALE_API_KEY")
        self._token = token or os.environ.get("ROTASCALE_TOKEN")

        # subhadipmitra@: Refuse at construction rather than on the first call.
        #
        # Without credentials this used to build happily and then fail on the
        # first authorisation with a bare 401 naming nothing — at which point
        # the agent is already running and somebody is reading a stack trace
        # instead of a sentence. A misconfigured governance client should say so
        # while the process is still starting up.
        if not self._api_key and not self._token:
            raise ValueError(
                "no credentials: pass api_key='rota_live_…' or set "
                "ROTASCALE_API_KEY. An API key is issued in the console under "
                "API keys, and names a workspace rather than an agent — one key "
                "serves your whole fleet."
            )
        if self._api_key and not self._api_key.startswith("rota_"):
            # Caught here rather than by the server, which deliberately says
            # only "api key rejected" and cannot tell you it looked wrong.
            raise ValueError(
                f"api_key does not look like a Rotascale key: expected it to "
                f"start with 'rota_', got {self._api_key[:6]!r}…"
            )
        self._timeout = timeout
        # subhadipmitra@: Enforcement gets a LONGER timeout than capture. Capture
        # is on the agent's critical path and can be dropped; an authorisation
        # decision cannot, so it is worth waiting a little longer for an answer
        # than for a receipt.
        self._enforcement_timeout = enforcement_timeout
        self._fail_open_enforcement = fail_open_enforcement
        self._workspace = workspace or os.environ.get("ROTASCALE_WORKSPACE")
        self._lock = threading.Lock()
        self._http: httpx.Client | None = None

    # --- transport --------------------------------------------------------

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            with self._lock:
                if self._http is None:
                    headers = {"user-agent": USER_AGENT}
                    # subhadipmitra@: Both go in `Authorization: Bearer`. The
                    # server accepts either there and tells them apart by the
                    # `rota_` prefix, so callers never have to work out which
                    # header a credential belongs in. The key wins if both are
                    # somehow set: it is the one an agent is meant to use.
                    credential = self._api_key or self._token
                    if credential:
                        headers["authorization"] = f"Bearer {credential}"
                    if self._workspace:
                        headers["x-rotascale-workspace"] = self._workspace
                    self._http = httpx.Client(
                        base_url=self.base_url, headers=headers, timeout=self._timeout
                    )
        return self._http

    def _post(self, path: str, body: dict, *, timeout: float | None = None) -> dict:
        response = self.http.post(path, json=body, timeout=timeout or self._timeout)
        response.raise_for_status()
        return response.json() if response.content else {}

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def __enter__(self) -> "Rotascale":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # --- registry ---------------------------------------------------------

    def register(
        self, name: str, *, owner: str, org_unit: str | None = None, tier: str = "L0", **kw: Any
    ) -> dict:
        """Register an agent. Idempotent by name within a workspace."""
        try:
            return self._post(
                "/v1/agents",
                {"name": name, "owner_subject": owner, "org_unit": org_unit,
                 "autonomy_tier": tier, **kw},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 422:
                body = exc.response.json()
                if body.get("existing_id"):
                    return {"id": body["existing_id"], "name": name, "existing": True}
            raise

    # --- identity ---------------------------------------------------------

    def agent(self, slug: str) -> Agent:
        """Name this agent. It is created on first sight if Rotascale has not
        seen the slug before.

        subhadipmitra@: The slug is a name YOU write and control, not an
        identifier you copy out of a console. It survives redeployment and is
        legible in a diff, which an opaque `agt_01KYY…` is not.

        Safe to call on every process start: the server maps `(workspace, slug)`
        to one agent and returns the same one thereafter.

        A newly discovered agent records evidence but **holds no authority** —
        somebody has to claim it in the console first. `agent.governed` says
        which state you are in, so an integration can warn at startup rather
        than discover it at the first authorisation.
        """
        seen = self._post("/v1/agents/resolve", {"slug": slug})
        agent = Agent(
            id=seen["id"],
            slug=seen["slug"],
            status=seen["status"],
            governed=seen["governed"],
        )
        if seen.get("notice"):
            # Warned once, at startup, where somebody is still watching the
            # logs — not on the first refusal, halfway through a shift.
            logger.warning("rotascale: %s (%s)", seen["notice"], agent.slug)
        return agent

    def report_provenance(
        self,
        agent: "Agent | str",
        *,
        model: str | None = None,
        provider: str | None = None,
        model_version: str | None = None,
        prompt_version: str | None = None,
        tools: dict[str, Any] | None = None,
        knowledge: dict[str, Any] | None = None,
    ) -> None:
        """Report this agent's current configuration.

        subhadipmitra@: The runtime already knows its model, its prompt version
        and its tool manifest. A human retyping a manifest digest into a form is
        recording a guess, and the grant-drift check that compares against it is
        then comparing against that guess.

        Called automatically by the middlewares the first time they see which
        model actually served a call — the SERVED identity, not the requested
        one, because those differ and only the served one is evidence.

        Deduplicated server-side on a content hash, so calling it on every
        process start is correct and cheap. Never raises: this is capture.
        """
        agent_id = agent.id if isinstance(agent, Agent) else agent
        body: dict[str, Any] = {
            "prompt_version": prompt_version,
            "tool_manifest": tools or {},
            "knowledge_sources": knowledge or {},
        }
        if model:
            body["model"] = {"name": model, "provider": provider,
                             "version": model_version}
        try:
            self._post(f"/v1/agents/{agent_id}/provenance", body)
        except Exception:
            logger.warning("rotascale: could not report provenance", exc_info=True)

    # --- capture ----------------------------------------------------------

    @contextmanager
    def witness(
        self,
        agent: str | Agent,
        *,
        ref: str | None = None,
        goal: dict[str, Any] | None = None,
        **kw: Any,
    ):
        """Open a trajectory for the duration of the block.

        Takes either an `Agent` from `rs.agent("slug")` or a raw agent id.

        Idempotent on `ref`: a retried request continues the same trajectory
        rather than forking history. On an exception the trajectory closes with
        status `failed` and the error recorded — a crash is evidence too.
        """
        _reject_near_misses(kw, "witness")
        agent_id = agent.id if isinstance(agent, Agent) else agent
        trajectory: Trajectory | None = None
        try:
            created = self._post(
                "/v1/trajectories",
                {"agent_id": agent_id, "external_ref": ref, "goal": goal or {}, **kw},
            )
            trajectory = Trajectory(self, created["id"], agent_id)
            # subhadipmitra@: `external_ref` is idempotent, so a retry — or a
            # reused ref — returns an EXISTING trajectory, which may already be
            # closed. Honour that: appending steps to a sealed record would
            # rightly be refused, and re-closing it 422s. Marking it closed here
            # turns a confusing cascade of errors into a quiet no-op, which is
            # what fail-open capture is supposed to feel like.
            if created.get("status") not in (None, "open"):
                logger.warning(
                    "rotascale: external_ref %r already refers to a %s trajectory (%s); "
                    "recording is a no-op for this block",
                    ref, created.get("status"), created["id"],
                )
                trajectory._closed = True
            trajectory._token = _current.set(trajectory)
        except Exception:
            logger.warning("rotascale: could not open a trajectory; running ungoverned-capture",
                           exc_info=True)
            trajectory = _NullTrajectory(self, agent_id)  # type: ignore[assignment]
            trajectory._token = _current.set(trajectory)

        try:
            yield trajectory
        except Exception as exc:
            trajectory.close(status="failed", error=type(exc).__name__, message=str(exc)[:500])
            raise
        else:
            trajectory.close()
        finally:
            if trajectory._token is not None:
                _current.reset(trajectory._token)

    # --- enforcement ------------------------------------------------------

    def authorize(
        self,
        grant_id: str,
        scope: dict[str, list[str]] | None = None,
        /,
        *,
        amount_minor: int = 0,
        currency: str | None = None,
        stakes_minor: int | None = None,
        trajectory_id: str | None = None,
        raise_on_refusal: bool = True,
        incumbent_decision: str | None = None,
        **action: Any,
    ) -> Decision:
        body = {
            "grant_id": grant_id,
            "action": {"scope": scope or {}, **action},
            "amount_minor": amount_minor,
            "currency": currency,
            "trajectory_id": trajectory_id,
        }
        if stakes_minor is not None:
            body["action"]["stakes_minor"] = stakes_minor
        # For grants in shadow mode: what your existing system or reviewer
        # decided. Divergence between that and the policy is the whole point of
        # a shadow run — where they disagree is where the policy is wrong.
        if incumbent_decision is not None:
            body["incumbent_decision"] = incumbent_decision

        try:
            raw = self._post("/v1/authorize", body, timeout=self._enforcement_timeout)
        except Exception as exc:
            if self._fail_open_enforcement:
                logger.error(
                    "rotascale: enforcement unreachable and fail_open_enforcement is ON — "
                    "this action is UNGOVERNED", exc_info=True,
                )
                return Decision("unavailable", True, "enforcement unreachable (failing open)")
            raise EnforcementUnavailable(
                f"could not reach Rotascale for an authorisation decision: {exc}"
            ) from exc

        decision = Decision(
            outcome=raw["outcome"],
            allowed=raw["allowed"],
            reason=raw["reason"],
            grant_id=raw.get("grant_id"),
            ledger_id=raw.get("ledger_id"),
            remaining_amount_minor=raw.get("remaining_amount_minor"),
            remaining_count=raw.get("remaining_count"),
            findings=raw.get("findings") or [],
            policy_outcome=raw.get("policy_outcome"),
            enforcement_mode=raw.get("enforcement_mode"),
        )
        _warn_if_not_enforcing(decision)
        if raise_on_refusal and not decision.allowed:
            raise _for(decision)
        return decision


# subhadipmitra@: Warned once per grant, not per call. A per-call warning in a
# hot path gets filtered out within the hour and then protects nobody; a single
# clear line at startup is read. Observe is a legitimate and recommended state —
# what must never happen is that it looks like enforcement.
_ANNOUNCED: set[str] = set()


def _warn_if_not_enforcing(decision: Decision) -> None:
    if decision.enforcing or not decision.grant_id:
        return
    if decision.grant_id in _ANNOUNCED:
        return
    _ANNOUNCED.add(decision.grant_id)
    logger.warning(
        "rotascale: grant %s is in %s mode and is NOT refusing anything. "
        "Decisions are being evaluated and recorded, and every one is being "
        "allowed through. Promote it to enforce when the recorded refusals look "
        "right. See the grant's rollout report.",
        decision.grant_id, decision.enforcement_mode,
    )


def _for(decision: Decision) -> Exception:
    """Map an outcome to an exception whose type tells the caller the remedy."""
    match decision.outcome:
        case "exhausted":
            return Exhausted(decision.reason, decision)
        case "gated":
            return Gated(decision.reason, decision)
        case "review_sync":
            return ReviewRequired(decision.reason, decision)
        case _:
            return Blocked(decision.reason, decision)


class _NullTrajectory(Trajectory):
    """Stand-in when the trajectory could not be opened.

    subhadipmitra@: Exists so `with rs.witness(...)` never explodes when the
    control plane is down. Every recording call becomes a no-op and the agent
    carries on. Enforcement still goes through the real client and still fails
    closed — losing evidence is survivable, losing the authority check is not.
    """

    def __init__(self, client: Rotascale, agent_id: str) -> None:
        super().__init__(client, trajectory_id="", agent_id=agent_id)

    def _record(self, kind: str, **body: Any) -> None:
        return

    def close(self, status: str = "completed", **outcome: Any) -> None:
        self._closed = True
