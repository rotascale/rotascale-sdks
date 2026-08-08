import { describe, expect, it, vi } from "vitest";

import {
  Blocked,
  Decision,
  EnforcementUnavailable,
  Exhausted,
  Gated,
  RequestRefused,
  ReviewRequired,
  Rotascale,
} from "../src/index.js";

/**
 * Capture never throws, enforcement always can.
 *
 * subhadipmitra@: `#63` asks for parity "on the surface that matters", and the
 * surface that matters is this. Two clients that disagree about which failures
 * stop an agent are worse than one client: a fleet written in both languages
 * then behaves two ways and nobody can say which is correct.
 *
 * These tests are written against the Python SDK's stated posture rather than
 * against this implementation, so they fail if TypeScript drifts.
 */

const silent = { error: vi.fn(), warn: vi.fn() };

function serving(payload: unknown, ok = true) {
  return vi.fn<typeof globalThis.fetch>(async () => new Response(JSON.stringify(payload), {
    status: ok ? 200 : 500,
    headers: { "content-type": "application/json" },
  }));
}

function unreachable() {
  return vi.fn<typeof globalThis.fetch>(async () => { throw new Error("ECONNREFUSED"); });
}

const ALLOW = {
  outcome: "allow", allowed: true, reason: "within bounds",
  grant_id: "grt_1", ledger_id: "led_1", enforcement_mode: "enforce",
  policy_outcome: "allow",
};

describe("enforcement fails closed", () => {
  it("throws rather than allowing when Rotascale cannot be reached", async () => {
    const client = new Rotascale({ fetch: unreachable(), logger: silent });

    await expect(client.authorize({ grantId: "grt_1" }))
      .rejects.toBeInstanceOf(EnforcementUnavailable);
  });

  it("fails open only when explicitly told to, and says so loudly", async () => {
    const logger = { error: vi.fn(), warn: vi.fn() };
    const client = new Rotascale({
      fetch: unreachable(), logger, failOpenEnforcement: true,
    });

    const decision = await client.authorize({ grantId: "grt_1" });

    expect(decision.allowed).toBe(true);
    expect(decision.outcome).toBe("unavailable");
    // Nobody should arrive at "my actions are ungoverned" by accident.
    expect(logger.error).toHaveBeenCalledWith(
      expect.stringContaining("UNGOVERNED"), expect.anything());
  });
});

/**
 * A refusal from a REACHABLE server is not an outage.
 *
 * subhadipmitra@: Found by running this SDK against the live API rather than
 * against these mocks. `grt_0000…` does not exist, the server returned 404,
 * and the client reported `EnforcementUnavailable` — so under
 * `failOpenEnforcement` the action was ALLOWED.
 *
 * The server refused and the client overruled it. Any agent could have passed
 * a garbage grant id and got through.
 *
 * The Python SDK has the same shape (`_post` raises `HTTPStatusError` and
 * `authorize` catches bare `Exception`), so this is filed against it too.
 */
describe("a refusal is not an outage", () => {
  it("does not fail open when the server answered", async () => {
    const client = new Rotascale({
      fetch: serving({ detail: "grant not found" }, false),
      failOpenEnforcement: true,
      logger: silent,
    });

    await expect(client.authorize({ grantId: "grt_nope" }))
      .rejects.toBeInstanceOf(RequestRefused);
  });

  it("reports the status, so a caller can tell 404 from 500", async () => {
    const client = new Rotascale({
      fetch: serving({ detail: "grant not found" }, false), logger: silent });

    await expect(client.authorize({ grantId: "grt_nope" }))
      .rejects.toMatchObject({ status: 500 });
  });

  it("still fails open on a genuine transport failure", async () => {
    const client = new Rotascale({
      fetch: unreachable(), failOpenEnforcement: true, logger: silent });

    const d = await client.authorize({ grantId: "grt_1" });
    expect(d.allowed).toBe(true);
    expect(d.outcome).toBe("unavailable");
  });
});

describe("capture fails open", () => {
  it("returns null rather than throwing when a trajectory cannot be opened",
    async () => {
      const client = new Rotascale({ fetch: unreachable(), logger: silent });
      // Losing evidence is bad; taking down a customer's agent is worse.
      await expect(client.openTrajectory({ agentId: "agt_1" }))
        .resolves.toBeNull();
    });

  it("swallows a failed step rather than stopping the agent", async () => {
    // subhadipmitra@: The realistic shape — the trajectory opens, then the
    // control plane goes away mid-run. A first version of this test built a
    // Trajectory through its own constructor and asserted the client's
    // `capture` directly, which exercised neither the handle nor the step.
    let calls = 0;
    const flaky = vi.fn<typeof globalThis.fetch>(async () => {
      calls += 1;
      if (calls === 1) {
        return new Response(JSON.stringify({ id: "trj_1" }), { status: 200 });
      }
      throw new Error("ECONNREFUSED");
    });

    const logger = { error: vi.fn(), warn: vi.fn() };
    const client = new Rotascale({ fetch: flaky, logger });
    const trajectory = await client.openTrajectory({ agentId: "agt_1" });
    expect(trajectory).not.toBeNull();

    await expect(trajectory!.step({ kind: "llm_call" })).resolves.toBeUndefined();
    await expect(trajectory!.close()).resolves.toBeUndefined();

    // Swallowed, but never silently: an operator has to be able to find out
    // that evidence went missing.
    expect(logger.error).toHaveBeenCalledTimes(2);
  });
});

describe("a refusal's TYPE tells the caller the remedy", () => {
  it.each([
    ["exhausted", Exhausted],
    ["gated", Gated],
    ["review_sync", ReviewRequired],
    ["review_async", ReviewRequired],
    ["deny", Blocked],
  ])("%s throws %s", async (outcome, type) => {
    const client = new Rotascale({
      fetch: serving({ outcome, allowed: false, reason: "no", grant_id: "grt_1",
                       enforcement_mode: "enforce" }),
      logger: silent,
    });

    await expect(client.authorize({ grantId: "grt_1" }))
      .rejects.toBeInstanceOf(type);
  });

  it("returns the refusal instead when asked to", async () => {
    const client = new Rotascale({
      fetch: serving({ outcome: "deny", allowed: false, reason: "out of scope" }),
      logger: silent,
    });

    const decision = await client.authorize({
      grantId: "grt_1", throwOnRefusal: false });
    expect(decision.allowed).toBe(false);
    expect(decision.outcome).toBe("deny");
  });
});

/**
 * The `#49` defect, which `#63` explicitly says must not be reintroduced here.
 *
 * subhadipmitra@: A grant in observe returns `allowed: true` for everything. A
 * caller reading only `allowed` cannot tell a control that PERMITTED an action
 * from a control that is NOT RUNNING.
 */
describe("enforcing and suppressed", () => {
  it("reports a grant in observe as not enforcing", () => {
    const d = new Decision({ ...ALLOW, enforcement_mode: "observe" });
    expect(d.enforcing).toBe(false);
  });

  it("treats an unknown mode as enforcing, which is the safer error", () => {
    // An older server sends no `enforcement_mode`. Assuming a control is OFF
    // when it is on is the more dangerous of the two mistakes.
    const d = new Decision({ outcome: "allow", allowed: true, reason: "ok" });
    expect(d.enforcing).toBe(true);
  });

  it("does not treat a mode it has never heard of as enforcement", () => {
    // The set of modes will grow. A caller who wrote `mode === "enforce"`
    // would silently start treating a new non-enforcing mode as enforcement,
    // which is why this property exists at all.
    const d = new Decision({ ...ALLOW, enforcement_mode: "some_future_rung" });
    expect(d.enforcing).toBe(false);
  });

  it("reports a suppressed refusal", () => {
    // The policy said no and the mode let it through. If this is ever true in
    // production, the control you believe is running is not.
    const d = new Decision({
      ...ALLOW, outcome: "allow", allowed: true,
      policy_outcome: "deny", enforcement_mode: "observe",
    });
    expect(d.suppressed).toBe(true);
  });

  it("falls back to findings against a server that sends no policy_outcome", () => {
    const d = new Decision({
      outcome: "allow", allowed: true, reason: "not enforced (observe)",
      findings: ["would_refuse:deny:over the ceiling"],
    });
    expect(d.suppressed).toBe(true);
  });

  it("is not suppressed when the policy and the outcome agree", () => {
    expect(new Decision(ALLOW).suppressed).toBe(false);
  });

  it("warns once per grant, not once per decision", async () => {
    const logger = { error: vi.fn(), warn: vi.fn() };
    const client = new Rotascale({
      fetch: serving({ ...ALLOW, enforcement_mode: "observe" }), logger,
    });

    await client.authorize({ grantId: "grt_1" });
    await client.authorize({ grantId: "grt_1" });
    await client.authorize({ grantId: "grt_1" });

    // A per-call warning would fire on every action and be filtered out of the
    // logs within a day — which is how a control that is not running becomes
    // invisible.
    expect(logger.warn).toHaveBeenCalledTimes(1);
    expect(logger.warn).toHaveBeenCalledWith(expect.stringContaining("observe"));
  });
});

describe("the request it actually sends", () => {
  it("puts stakes_minor inside the action, where the bounds read it", async () => {
    const fetchMock = serving(ALLOW);
    const client = new Rotascale({ fetch: fetchMock, logger: silent });

    await client.authorize({
      grantId: "grt_1", scope: { tools: ["issue_refund"] },
      amountMinor: 4500, currency: "EUR", stakesMinor: 4500,
    });

    const body = JSON.parse(String(fetchMock.mock.calls[0]![1]!.body));
    expect(body.action.stakes_minor).toBe(4500);
    expect(body.action.scope).toEqual({ tools: ["issue_refund"] });
    expect(body.amount_minor).toBe(4500);
  });

  it("does not mark a source trusted unless the caller says so", async () => {
    const fetchMock = serving({ id: "trj_1" });
    const client = new Rotascale({ fetch: fetchMock, logger: silent });
    const trajectory = await client.openTrajectory({ agentId: "agt_1" });
    await trajectory!.step({ kind: "retrieval", sourceRef: "upload:DOC" });

    const body = JSON.parse(String(fetchMock.mock.calls[1]![1]!.body));
    // Defaulting this to true would quietly disable information-flow control
    // for every TypeScript agent.
    expect(body.trusted_source).toBe(false);
  });

  it("sends the incumbent decision for a shadow run", async () => {
    const fetchMock = serving(ALLOW);
    const client = new Rotascale({ fetch: fetchMock, logger: silent });

    await client.authorize({ grantId: "grt_1", incumbentDecision: "allow" });

    const body = JSON.parse(String(fetchMock.mock.calls[0]![1]!.body));
    expect(body.incumbent_decision).toBe("allow");
  });
});
