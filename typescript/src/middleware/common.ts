/**
 * Shared behaviour for every middleware.
 *
 * subhadipmitra@: Middleware is CAPTURE, so nothing here may throw. A model
 * call that succeeded must not fail because recording it did — that inverts the
 * whole failure posture, and an agent that dies while its provider is healthy
 * is the worst outcome this SDK can produce.
 */

import type { Trajectory } from "../client.js";

/** Where the middleware attaches steps, set by `witness()`. */
let current: Trajectory | null = null;

export function currentTrajectory(): Trajectory | null {
  return current;
}

/**
 * Run `fn` with a trajectory in scope, so wrapped clients attach to it.
 *
 * subhadipmitra@: Node has no contextvars, so this is a module-level handle
 * rather than the Python SDK's `ContextVar`. That is a real difference and it
 * is stated rather than papered over: concurrent runs in one process would
 * share it, so a server handling several agents at once should pass
 * `trajectoryId` explicitly instead.
 *
 * `AsyncLocalStorage` would fix that and is the right answer when this needs
 * to hold under concurrency — deliberately not reached for yet, because it
 * costs a `node:async_hooks` import in a package that currently runs anywhere.
 */
export async function witness<T>(
  trajectory: Trajectory | null,
  fn: () => Promise<T>,
): Promise<T> {
  const previous = current;
  current = trajectory;
  try {
    return await fn();
  } finally {
    current = previous;
  }
}

/** Record a step, swallowing anything that goes wrong. Capture never throws. */
export async function record(
  kind: string, payload: Record<string, unknown>,
): Promise<void> {
  const trajectory = current;
  if (trajectory === null) return;
  try {
    await trajectory.step({ kind, payload });
  } catch {
    // `Trajectory.step` already swallows and logs. This is the belt for the
    // braces: a middleware must not be the thing that stops an agent.
  }
}

/**
 * Cut a captured string to length.
 *
 * subhadipmitra@: Content capture is off by default and bounded when on. A
 * middleware that recorded whole prompts by default would put customer data in
 * our store because somebody imported a wrapper, which is not a decision an
 * import should make.
 */
export function truncate(value: unknown, limit: number): unknown {
  if (typeof value !== "string") return value;
  return value.length <= limit ? value : `${value.slice(0, limit)}…[truncated]`;
}

/**
 * What actually served the call, reported once per (model, provider).
 *
 * subhadipmitra@: The inventory learns what RAN from the runtime that ran it,
 * rather than from what somebody declared. Once per pair, not once per call —
 * a report on every completion would be a request per completion.
 */
const reported = new Set<string>();

export function seenServedModel(
  model: string | null | undefined, provider: string,
): boolean {
  if (!model) return false;
  const key = `${provider}:${model}`;
  if (reported.has(key)) return false;
  reported.add(key);
  return true;
}
