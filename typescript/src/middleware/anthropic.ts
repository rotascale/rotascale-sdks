/**
 * Anthropic middleware.
 *
 *     const client = watchAnthropic(new Anthropic());
 *     await witness(run, async () => {
 *       await client.messages.create({ model: "claude-…", messages, max_tokens: 1024 });
 *     });
 *
 * subhadipmitra@: Duck-typed, like every middleware here — this module imports
 * no provider library and wraps anything exposing `messages.create`.
 *
 * ## The usage field names, which are the point of this file
 *
 * Anthropic reports `input_tokens` / `output_tokens`. Every other middleware
 * writes `prompt_tokens` / `completion_tokens`. The Python SDK recorded
 * Anthropic's spelling verbatim, so usage could not be summed across providers
 * — right numbers under the wrong names, which is the hardest kind of wrong to
 * notice because nothing is missing and nothing errors.
 *
 * It survived its own unit tests: the fake in those tests had the same mistake
 * baked in, so the test agreed with the bug. Only a call against the real API
 * found it (`#88`).
 *
 * So this normalises at the boundary, and `test/middleware.test.ts` asserts the
 * normalisation rather than the spelling.
 */

import { record, seenServedModel, truncate } from "./common.js";
import type { WatchOptions } from "./openai-compat.js";

type Messages = { create(args: Record<string, unknown>): Promise<unknown> };
type AnthropicClient = { messages: Messages };

function get(o: unknown, key: string): unknown {
  return o && typeof o === "object" ? (o as Record<string, unknown>)[key] : undefined;
}

export function watchAnthropic<T extends AnthropicClient>(
  client: T, options: WatchOptions = {},
): T {
  const capture = options.captureContent ?? false;
  const limit = options.limit ?? 2_000;
  const inner = client.messages;

  const watched: Messages = {
    async create(args: Record<string, unknown>): Promise<unknown> {
      const started = Date.now();
      let response: unknown;
      try {
        response = await inner.create(args);
      } catch (error) {
        await record("llm_call", {
          provider: "anthropic",
          model_requested: args.model ?? null,
          latency_ms: Date.now() - started,
          failed: true,
          error: String(error).slice(0, 500),
        });
        throw error;
      }

      const step: Record<string, unknown> = {
        provider: "anthropic",
        model_requested: args.model ?? null,
        temperature: args.temperature ?? null,
        latency_ms: Date.now() - started,
        model_served: get(response, "model") ?? null,
        response_id: get(response, "id") ?? null,
        stop_reason: get(response, "stop_reason") ?? null,
      };

      if (seenServedModel(step.model_served as string, "anthropic")) {
        step.first_seen_served_model = true;
      }

      const usage = get(response, "usage");
      if (usage) {
        step.usage = {
          // NORMALISED. See the module docstring — Anthropic calls these
          // input/output, and recording that spelling makes usage
          // unsummable across providers.
          prompt_tokens: get(usage, "input_tokens") ?? null,
          completion_tokens: get(usage, "output_tokens") ?? null,
          // Kept under Anthropic's own name because nothing else has it, and
          // it is billed separately — a cache read is not a prompt token.
          cache_read_tokens: get(usage, "cache_read_input_tokens") ?? null,
        };
      }

      const content = get(response, "content");
      if (Array.isArray(content)) {
        const tools = content
          .filter((b) => get(b, "type") === "tool_use")
          .map((b) => get(b, "name") ?? null);
        if (tools.length > 0) step.tool_calls = tools;

        if (capture) {
          const text = content
            .filter((b) => get(b, "type") === "text")
            .map((b) => get(b, "text"))
            .join("");
          step.response = truncate(text, limit);
        }
      }

      if (capture && Array.isArray(args.messages) && args.messages.length > 0) {
        const last = args.messages[args.messages.length - 1];
        step.last_message = truncate(get(last, "content") ?? last, limit);
      }

      await record("llm_call", step);
      return response;
    },
  };

  return new Proxy(client, {
    get(target, prop, receiver) {
      if (prop === "messages") return watched;
      return Reflect.get(target, prop, receiver);
    },
  });
}
