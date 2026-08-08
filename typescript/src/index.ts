/**
 * Rotascale — govern the action, not the model.
 *
 * subhadipmitra@: The two things a caller must be able to reach without
 * reading the source are the failure posture and the `enforcing` /
 * `suppressed` pair. Both are exported here rather than buried, because a
 * client that makes them hard to find is a client that gets used wrongly.
 */
export { Rotascale, Trajectory, refusalFor } from "./client.js";
export type { RotascaleOptions, AuthorizeOptions } from "./client.js";
export { Decision } from "./decision.js";
export type { DecisionPayload } from "./decision.js";
export {
  RotascaleError,
  Blocked,
  Exhausted,
  Gated,
  ReviewRequired,
  EnforcementUnavailable,
  RequestRefused,
} from "./errors.js";
