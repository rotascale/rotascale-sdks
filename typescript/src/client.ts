/**
 * The TypeScript client.
 *
 * subhadipmitra@: `#63` says parity "on the surface that matters", and the
 * surface that matters is not the method list — it is the FAILURE POSTURE.
 * Two clients that disagree about which failures stop an agent are worse than
 * one client, because a fleet written in both languages then behaves two ways
 * and nobody can say which is correct.
 *
 * So: capture never throws, enforcement always can. Every recording call
 * swallows its error and logs; `authorize` throws `EnforcementUnavailable`
 * when the control plane cannot be reached.
 *
 * The issue also warns: *"Every governance derivation still living in the
 * console is one a second SDK would have to reimplement."* Nothing here
 * derives a governance fact. `enforcing` and `suppressed` read fields the
 * server sends; there is no second opinion about what a refusal means.
 */

import { Decision, type DecisionPayload } from "./decision.js";
import {
  Blocked,
  EnforcementUnavailable,
  Exhausted,
  Gated,
  RequestRefused,
  ReviewRequired,
} from "./errors.js";

export interface RotascaleOptions {
  /** Defaults to `ROTASCALE_API_URL`, then `https://api.rotascale.com`. */
  baseUrl?: string;
  /** Defaults to `ROTASCALE_API_KEY`. */
  apiKey?: string;
  /** Milliseconds. Enforcement is on the hot path, so it is short by default. */
  enforcementTimeoutMs?: number;
  /** Capture may take longer: it is off the critical path by construction. */
  captureTimeoutMs?: number;
  /**
   * Let actions through when Rotascale cannot be reached.
   *
   * subhadipmitra@: OFF by default, and it logs every single time it is used.
   * A caller may decide availability matters more than governance for their
   * workload; nobody should arrive at that position by accident.
   */
  failOpenEnforcement?: boolean;
  /** Swap in for tests. Defaults to global `fetch`. */
  fetch?: typeof globalThis.fetch;
  /** Swap in for tests. Defaults to `console`. */
  logger?: Pick<Console, "error" | "warn">;
}

export interface AuthorizeOptions {
  grantId: string;
  scope?: Record<string, string[]>;
  amountMinor?: number;
  currency?: string;
  stakesMinor?: number;
  trajectoryId?: string;
  /** Throw on a refusal rather than returning it. On by default. */
  throwOnRefusal?: boolean;
  /**
   * For grants in shadow mode: what your existing system decided.
   * Divergence between that and the policy is the whole point of a shadow run.
   */
  incumbentDecision?: string;
  /** Anything else the action declares — `record_count`, custom fields. */
  action?: Record<string, unknown>;
}

const DEFAULT_BASE = "https://api.rotascale.com";

export class Rotascale {
  private readonly baseUrl: string;
  private readonly apiKey: string | undefined;
  private readonly enforcementTimeoutMs: number;
  private readonly captureTimeoutMs: number;
  private readonly failOpenEnforcement: boolean;
  private readonly doFetch: typeof globalThis.fetch;
  private readonly log: Pick<Console, "error" | "warn">;

  constructor(options: RotascaleOptions = {}) {
    const env = (globalThis as { process?: { env?: Record<string, string | undefined> } })
      .process?.env ?? {};
    this.baseUrl = (options.baseUrl ?? env.ROTASCALE_API_URL ?? DEFAULT_BASE)
      .replace(/\/+$/, "");
    this.apiKey = options.apiKey ?? env.ROTASCALE_API_KEY;
    this.enforcementTimeoutMs = options.enforcementTimeoutMs ?? 5_000;
    this.captureTimeoutMs = options.captureTimeoutMs ?? 10_000;
    this.failOpenEnforcement = options.failOpenEnforcement ?? false;
    this.doFetch = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.log = options.logger ?? console;
  }

  private async post<T>(path: string, body: unknown, timeoutMs: number): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await this.doFetch(`${this.baseUrl}${path}`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          ...(this.apiKey ? { authorization: `Bearer ${this.apiKey}` } : {}),
        },
        body: JSON.stringify(body),
        signal: controller.signal,
      });
      const text = await response.text();
      const parsed = text ? JSON.parse(text) : null;
      if (!response.ok) {
        // subhadipmitra@: A distinct type, because the caller's next move
        // differs. RFC 7807 across the whole API, so `detail` is the sentence.
        const detail = parsed?.detail ?? parsed?.title ?? `HTTP ${response.status}`;
        throw new RequestRefused(`${response.status}: ${detail}`, response.status);
      }
      return parsed as T;
    } finally {
      clearTimeout(timer);
    }
  }

  /**
   * Ask whether an action may happen.
   *
   * Throws `EnforcementUnavailable` if Rotascale cannot be reached — see the
   * module docstring. Throws a `Blocked` subclass on a refusal unless
   * `throwOnRefusal` is false.
   */
  async authorize(options: AuthorizeOptions): Promise<Decision> {
    const action: Record<string, unknown> = {
      scope: options.scope ?? {},
      ...(options.action ?? {}),
    };
    if (options.stakesMinor !== undefined) {
      action.stakes_minor = options.stakesMinor;
    }

    const body: Record<string, unknown> = {
      grant_id: options.grantId,
      action,
      // subhadipmitra@: ZERO, not null. `AuthorizeIn.amount_minor` is
      // `int = Field(default=0, ge=0)` — a non-optional int with a default, so
      // an explicit null is a 422 while an absent field is fine.
      //
      // This client sent `null` and every authorize without an amount was
      // rejected. Nothing caught it: the unit tests mock the server, so they
      // validate the shape this file believes in rather than the shape the API
      // accepts. Found by running against the live API, which is why
      // `test/contract.test.ts` now checks the body against the real schema.
      //
      // `currency` and `trajectory_id` ARE `| None`, so null is correct there.
      amount_minor: options.amountMinor ?? 0,
      currency: options.currency ?? null,
      trajectory_id: options.trajectoryId ?? null,
    };
    if (options.incumbentDecision !== undefined) {
      body.incumbent_decision = options.incumbentDecision;
    }

    let raw: DecisionPayload;
    try {
      raw = await this.post<DecisionPayload>(
        "/v1/authorize", body, this.enforcementTimeoutMs);
    } catch (cause) {
      // subhadipmitra@: The server ANSWERED. Failing open here would let a
      // nonexistent grant id through, which is what happened: `grt_0000…`
      // returned 404, the SDK read it as an outage, and the action was
      // allowed. Fail-open covers an unreachable control plane, never a
      // refusal from a reachable one.
      if (cause instanceof RequestRefused) {
        throw cause;
      }
      if (this.failOpenEnforcement) {
        this.log.error(
          "rotascale: enforcement unreachable and failOpenEnforcement is ON — " +
          "this action is UNGOVERNED", cause);
        return new Decision({
          outcome: "unavailable",
          allowed: true,
          reason: "enforcement unreachable (failing open)",
        });
      }
      throw new EnforcementUnavailable(
        `could not reach Rotascale for an authorisation decision: ${String(cause)}`);
    }

    const decision = new Decision(raw);
    this.warnIfNotEnforcing(decision);

    if ((options.throwOnRefusal ?? true) && !decision.allowed) {
      throw refusalFor(decision);
    }
    return decision;
  }

  /**
   * subhadipmitra@: Warned about ONCE per grant, not per decision.
   *
   * A grant in observe returns `allow` for everything, so a per-call warning
   * would emit on every action and be filtered out of the logs within a day —
   * which is how a control that is not running becomes invisible.
   */
  private readonly warned = new Set<string>();

  private warnIfNotEnforcing(decision: Decision): void {
    if (decision.enforcing || decision.grantId === null) return;
    if (this.warned.has(decision.grantId)) return;
    this.warned.add(decision.grantId);
    this.log.warn(
      `rotascale: grant ${decision.grantId} is in ${decision.enforcementMode} — ` +
      `it is measuring, not refusing. Decisions from it are not a control.`);
  }

  // --- capture: never throws -------------------------------------------------

  /**
   * Open a trajectory. Returns `null` if capture failed.
   *
   * subhadipmitra@: `null` rather than a throw, and rather than a fake handle.
   * Losing evidence is bad and taking down a customer's agent is worse — but a
   * handle that silently records nothing would be worst, because the agent
   * would carry on believing it was governed.
   */
  async openTrajectory(input: {
    agentId: string;
    externalRef?: string;
    goal?: Record<string, unknown>;
  }): Promise<Trajectory | null> {
    try {
      const raw = await this.post<{ id: string }>("/v1/trajectories", {
        agent_id: input.agentId,
        external_ref: input.externalRef ?? null,
        goal: input.goal ?? {},
      }, this.captureTimeoutMs);
      return new Trajectory(this, raw.id);
    } catch (cause) {
      this.log.error("rotascale: could not open a trajectory — continuing "
        + "without capture", cause);
      return null;
    }
  }

  /** @internal Capture calls route through here so all of them fail open. */
  async capture(path: string, body: unknown): Promise<void> {
    try {
      await this.post(path, body, this.captureTimeoutMs);
    } catch (cause) {
      this.log.error(`rotascale: capture failed for ${path} — continuing`, cause);
    }
  }

  /** Report what an agent is running, so conformance can be checked. */
  async reportProvenance(agentId: string, provenance: Record<string, unknown>):
    Promise<void> {
    await this.capture(`/v1/agents/${agentId}/provenance`, provenance);
  }
}

/**
 * One run of an agent, recorded as it happens.
 *
 * Every method here is capture, so none of them throw.
 */
export class Trajectory {
  constructor(private readonly client: Rotascale, readonly id: string) {}

  async step(input: {
    kind: string;
    payload?: Record<string, unknown>;
    grantId?: string;
    sourceRef?: string;
    /**
     * The customer has attested this source as safe.
     *
     * subhadipmitra@: A trusted read introduces NO taint label at all, so this
     * is an attestation and therefore itself evidence. Defaulting it to true
     * would quietly disable information-flow control for every TypeScript
     * agent, which is why it defaults to false and is named rather than
     * inferred.
     */
    trustedSource?: boolean;
    discharges?: string[];
  }): Promise<void> {
    await this.client.capture(`/v1/trajectories/${this.id}/steps`, {
      kind: input.kind,
      payload: input.payload ?? {},
      grant_id: input.grantId ?? null,
      source_ref: input.sourceRef ?? "",
      trusted_source: input.trustedSource ?? false,
      discharges: input.discharges ?? [],
    });
  }

  async close(input: { outcome?: Record<string, unknown>; status?: string } = {}):
    Promise<void> {
    await this.client.capture(`/v1/trajectories/${this.id}/close`, {
      outcome: input.outcome ?? {},
      status: input.status ?? "completed",
    });
  }
}

/**
 * Map an outcome to an exception whose TYPE tells the caller the remedy.
 *
 * subhadipmitra@: The type is the message. `Exhausted` means somebody must
 * raise a budget; `Gated` means the context was tainted and needs a sanitiser
 * or an approval; `Blocked` means the scope is wrong. A caller catching one
 * `Blocked` for all three would have to parse a string to know what to do.
 */
export function refusalFor(decision: Decision): Error {
  switch (decision.outcome) {
    case "exhausted":
      return new Exhausted(decision.reason, decision);
    case "gated":
      return new Gated(decision.reason, decision);
    case "review_sync":
    case "review_async":
      return new ReviewRequired(decision.reason, decision);
    default:
      return new Blocked(decision.reason, decision);
  }
}
