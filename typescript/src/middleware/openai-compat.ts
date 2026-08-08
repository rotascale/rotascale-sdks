/**
 * OpenAI-compatible middleware.
 *
 * Wraps anything exposing `chat.completions.create` — the OpenAI SDK, Azure
 * OpenAI, Together, Groq, vLLM, **Ollama**, and every other service that copied
 * the shape.
 *
 *     const client = watchOpenAI(new OpenAI({ baseURL: "http://localhost:11434/v1" }));
 *     await witness(run, async () => {
 *       await client.chat.completions.create({ model: "llama3.2:3b", messages });
 *       // the call is on the trajectory; no other change to the agent
 *     });
 *
 * subhadipmitra@: DUCK-TYPED, and named `openai-compat` rather than `openai`
 * for the same reason the Python one is: this module imports no provider
 * library and never will. An enterprise running its own models on its own
 * hardware is not a special case to support later — it is the same code path,
 * and calling the file `openai` would imply otherwise.
 *
 * Rotascale governs the ACTION, not the model. Which model produced a request
 * is something the record notes, never something the decision depends on.
 */

import { record, seenServedModel, truncate } from "./common.js";

export interface WatchOptions {
  /**
   * Record prompt and response text.
   *
   * subhadipmitra@: OFF by default. A middleware that captured whole prompts
   * because somebody imported it would put customer data in the evidence store
   * as a side effect of an import, which is not a decision an import may make.
   */
  captureContent?: boolean;
  /** Characters kept per captured field when `captureContent` is on. */
  limit?: number;
}

type Completions = { create(args: Record<string, unknown>): Promise<unknown> };
type ChatClient = { chat: { completions: Completions } };

function get(o: unknown, key: string): unknown {
  return o && typeof o === "object" ? (o as Record<string, unknown>)[key] : undefined;
}

/**
 * Wrap a client so every completion lands on the current trajectory.
 *
 * The returned object delegates everything else to the original, so this is a
 * drop-in: no other change to the agent.
 */
export function watchOpenAI<T extends ChatClient>(
  client: T, options: WatchOptions = {},
): T {
  const capture = options.captureContent ?? false;
  const limit = options.limit ?? 2_000;
  const inner = client.chat.completions;

  const watched: Completions = {
    async create(args: Record<string, unknown>): Promise<unknown> {
      const started = Date.now();
      let response: unknown;
      try {
        response = await inner.create(args);
      } catch (error) {
        // subhadipmitra@: A FAILED call is evidence too. "The agent did nothing
        // because the provider was down" is a materially different story from
        // "the agent chose to do nothing", and only one of them is in the
        // record if failures are dropped.
        await record("llm_call", {
          provider: "openai-compatible",
          model_requested: args.model ?? null,
          latency_ms: Date.now() - started,
          failed: true,
          error: String(error).slice(0, 500),
        });
        throw error;
      }

      const step: Record<string, unknown> = {
        provider: "openai-compatible",
        model_requested: args.model ?? null,
        temperature: args.temperature ?? null,
        tool_choice: args.tool_choice ?? null,
        latency_ms: Date.now() - started,
        model_served: get(response, "model") ?? null,
        response_id: get(response, "id") ?? null,
      };

      // The inventory learns what actually ran, from the runtime that ran it.
      if (seenServedModel(step.model_served as string, "openai-compatible")) {
        step.first_seen_served_model = true;
      }

      const usage = get(response, "usage");
      if (usage) {
        step.usage = {
          prompt_tokens: get(usage, "prompt_tokens") ?? null,
          completion_tokens: get(usage, "completion_tokens") ?? null,
        };
      }

      const choices = get(response, "choices");
      const first = Array.isArray(choices) ? choices[0] : undefined;
      if (first) {
        step.finish_reason = get(first, "finish_reason") ?? null;
        const message = get(first, "message");
        const calls = get(message, "tool_calls");
        if (Array.isArray(calls) && calls.length > 0) {
          // Tool NAMES only. The arguments are the agent's business and land on
          // their own tool_call steps if the agent records them.
          step.tool_calls = calls.map((c) => get(get(c, "function"), "name") ?? null);
        }
        if (capture && message) {
          step.response = truncate(get(message, "content"), limit);
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

  // subhadipmitra@: A PROXY rather than a rebuilt object, so anything else on
  // the client — `embeddings`, `models`, a provider-specific extension —
  // continues to work untouched. Rebuilding would silently drop whatever this
  // file had not heard of, which is every future API.
  return new Proxy(client, {
    get(target, prop, receiver) {
      if (prop === "chat") {
        return new Proxy(target.chat, {
          get(chatTarget, chatProp, chatReceiver) {
            if (chatProp === "completions") return watched;
            return Reflect.get(chatTarget, chatProp, chatReceiver);
          },
        });
      }
      return Reflect.get(target, prop, receiver);
    },
  });
}
