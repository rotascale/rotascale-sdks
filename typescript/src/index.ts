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

/**
 * Middleware. Duck-typed, no provider library imported by any of them.
 *
 * subhadipmitra@: `openai-compat` rather than `openai`, deliberately. It wraps
 * anything speaking that shape — Azure, Together, Groq, vLLM, Ollama — so an
 * enterprise running its own models on its own hardware is the same code path
 * rather than a special case. Rotascale governs the action, not the model.
 */
export { watchOpenAI } from "./middleware/openai-compat.js";
export type { WatchOptions } from "./middleware/openai-compat.js";
export { watchAnthropic } from "./middleware/anthropic.js";
export { witness, currentTrajectory } from "./middleware/common.js";
