/**
 * SDK exceptions.
 *
 * The split that matters: **capture never throws, enforcement always can.**
 *
 * A recording problem must not break a customer's production agent — evidence
 * is worth a lot, but not an outage. An authorisation problem must stop the
 * agent, because an ungoverned action is worse than a delayed one.
 *
 * subhadipmitra@: Mirrored from the Python SDK deliberately, name for name. A
 * second client is only useful if the two agree about what happened, and the
 * one thing that must never differ between them is which failures stop an
 * agent — a TypeScript caller who learned that a taint gate throws `Blocked`
 * would be wrong about the remedy.
 */

export class RotascaleError extends Error {
  constructor(message: string) {
    super(message);
    this.name = new.target.name;
  }
}

/**
 * The action was refused: out of scope, past a ceiling, expired, or revoked.
 *
 * This must stop the agent. It is not retryable — the answer will be the same
 * until a human changes the grant.
 */
export class Blocked extends RotascaleError {
  constructor(message: string, readonly decision?: Decision) {
    super(message);
  }
}

/**
 * The grant's budget or call count is spent.
 *
 * Separate from `Blocked` because the remedy differs: somebody must raise the
 * budget or issue a new grant, rather than change the scope.
 */
export class Exhausted extends Blocked {}

/**
 * Refused because the trajectory's context is tainted and this grant requires
 * a clean one.
 *
 * The agent read something untrusted and then tried to act with authority. The
 * remedy is a human approval or a declared sanitiser — not a retry.
 */
export class Gated extends Blocked {}

/**
 * A human must decide before the action runs.
 *
 * The queue item already exists server-side; park the action and return.
 */
export class ReviewRequired extends RotascaleError {
  constructor(message: string, readonly decision?: Decision) {
    super(message);
  }
}

/**
 * Rotascale could not be reached for an authorisation decision.
 *
 * subhadipmitra@: Deliberately an exception rather than a silent allow.
 * Capture fails open; enforcement fails CLOSED. If the control plane is
 * unreachable the honest position is that the action is ungoverned, and an
 * ungoverned action is the thing this product exists to prevent.
 *
 * `failOpenEnforcement` exists for callers who have decided otherwise, and it
 * logs loudly every time it is used.
 */
export class EnforcementUnavailable extends RotascaleError {}

/**
 * Rotascale answered, and the request itself was wrong.
 *
 * subhadipmitra@: DISTINCT from `EnforcementUnavailable`, and the distinction
 * is a security property rather than tidiness.
 *
 * `EnforcementUnavailable` means the control plane could not be reached, so
 * the caller has to decide whether to proceed ungoverned — and a caller with
 * `failOpenEnforcement` proceeds. This means the control plane WAS reached and
 * said no: an unknown grant, a malformed action, a revoked key.
 *
 * Collapsing the two let a nonexistent grant id through under fail-open.
 * Verified against the live API: `grt_0000…` returned 404, the SDK read that
 * as an outage, and the action was allowed. The server refused and the client
 * overruled it.
 */
export class RequestRefused extends RotascaleError {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

import type { Decision } from "./decision.js";
