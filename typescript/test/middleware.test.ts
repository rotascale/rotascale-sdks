import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  Rotascale,
  watchAnthropic,
  watchOpenAI,
  witness,
} from "../src/index.js";

/**
 * Middleware is capture, so none of it may throw — and the numbers it records
 * have to mean the same thing across providers.
 *
 * subhadipmitra@: The second half is the one that bit. Anthropic reports
 * `input_tokens`/`output_tokens`; every other middleware writes
 * `prompt_tokens`/`completion_tokens`. The Python SDK recorded Anthropic's
 * spelling verbatim, so usage could not be summed across a fleet — right
 * numbers under the wrong names, which is the hardest kind of wrong to notice.
 *
 * It survived its own unit tests, because the fake in those tests had the same
 * mistake baked in and the test agreed with the bug. Only a call against the
 * real API found it (`#88`). So these assert the NORMALISED names against a
 * fake that speaks the provider's actual spelling.
 */

const silent = { error: vi.fn(), warn: vi.fn() };

/** Captures every step the middleware records. */
function recorder() {
  const steps: Array<Record<string, unknown>> = [];
  const fetchMock = vi.fn<typeof globalThis.fetch>(async (url, init) => {
    const path = String(url);
    if (path.endsWith("/steps")) {
      steps.push(JSON.parse(String(init!.body)));
      return new Response("{}", { status: 200 });
    }
    return new Response(JSON.stringify({ id: "trj_1" }), { status: 200 });
  });
  return { steps, fetchMock };
}

async function run(fn: (t: any) => Promise<void>) {
  const { steps, fetchMock } = recorder();
  const client = new Rotascale({ baseUrl: "https://rotagrant.test",fetch: fetchMock, logger: silent });
  const trajectory = await client.openTrajectory({ agentId: "agt_1" });
  await witness(trajectory, async () => fn(trajectory));
  return steps;
}

beforeEach(() => vi.clearAllMocks());

// --- the usage names, normalised at the boundary ----------------------------

describe("anthropic usage is normalised", () => {
  it("writes prompt_tokens and completion_tokens, not Anthropic's spelling",
    async () => {
      // The fake speaks the PROVIDER's language on purpose. A fake that already
      // said prompt_tokens would agree with the bug, which is exactly how the
      // Python one survived its tests.
      const anthropic = {
        messages: {
          create: async (_args?: Record<string, unknown>) => ({
            id: "msg_1", model: "claude-haiku-4-5", stop_reason: "end_turn",
            usage: { input_tokens: 120, output_tokens: 45,
                     cache_read_input_tokens: 80 },
            content: [{ type: "text", text: "hello" }],
          }),
        },
      };

      const steps = await run(async () => {
        await watchAnthropic(anthropic).messages.create({
          model: "claude-haiku-4-5", messages: [{ role: "user", content: "hi" }],
        });
      });

      const usage = steps.at(-1)!.payload as any;
      expect(usage.usage.prompt_tokens).toBe(120);
      expect(usage.usage.completion_tokens).toBe(45);
      expect(usage.usage).not.toHaveProperty("input_tokens");
      expect(usage.usage).not.toHaveProperty("output_tokens");
    });

  it("keeps cache_read_tokens under its own name", async () => {
    // Nothing else has it and it is billed separately: a cache read is not a
    // prompt token, so folding it in would overstate prompt usage.
    const anthropic = {
      messages: {
        create: async (_args?: Record<string, unknown>) => ({
          usage: { input_tokens: 10, output_tokens: 5, cache_read_input_tokens: 900 },
          content: [],
        }),
      },
    };

    const steps = await run(async () => {
      await watchAnthropic(anthropic).messages.create({ model: "m", messages: [] });
    });

    expect((steps.at(-1)!.payload as any).usage.cache_read_tokens).toBe(900);
  });

  it("the two providers agree on the field names", async () => {
    const openai = {
      chat: { completions: { create: async (_args?: Record<string, unknown>) => ({
        model: "gpt-4o-mini", usage: { prompt_tokens: 7, completion_tokens: 3 },
        choices: [{ finish_reason: "stop", message: { content: "x" } }],
      }) } },
    };
    const anthropic = {
      messages: { create: async (_args?: Record<string, unknown>) => ({
        model: "claude-haiku-4-5",
        usage: { input_tokens: 7, output_tokens: 3 }, content: [],
      }) },
    };

    const steps = await run(async () => {
      await watchOpenAI(openai).chat.completions.create({ model: "m", messages: [] });
      await watchAnthropic(anthropic).messages.create({ model: "m", messages: [] });
    });

    const [a, b] = steps.slice(-2).map((s) => (s.payload as any).usage);
    // The whole point: a fleet's usage can be summed without knowing which
    // provider served which call.
    expect(Object.keys(a).sort()).toEqual(["completion_tokens", "prompt_tokens"]);
    expect(b.prompt_tokens).toBe(a.prompt_tokens);
    expect(b.completion_tokens).toBe(a.completion_tokens);
  });
});

// --- a self-hosted model is the same code path ------------------------------

describe("it does not care which service answered", () => {
  it("records an Ollama call exactly like an OpenAI one", async () => {
    // subhadipmitra@: The reason the file is `openai-compat` and not `openai`.
    // Nothing in the middleware knows or asks where the model runs — an
    // enterprise on its own hardware is this path, not a future one.
    const ollama = {
      chat: { completions: { create: async (_args?: Record<string, unknown>) => ({
        id: "chatcmpl-local", model: "llama3.2:3b",
        usage: { prompt_tokens: 31, completion_tokens: 12 },
        choices: [{ finish_reason: "stop", message: { content: "hi" } }],
      }) } },
    };

    const steps = await run(async () => {
      await watchOpenAI(ollama).chat.completions.create({
        model: "llama3.2:3b", messages: [{ role: "user", content: "hi" }],
      });
    });

    const p = steps.at(-1)!.payload as any;
    expect(p.provider).toBe("openai-compatible");
    expect(p.model_served).toBe("llama3.2:3b");
    expect(p.usage.prompt_tokens).toBe(31);
  });
});

// --- capture never breaks the agent -----------------------------------------

describe("capture never breaks the agent", () => {
  it("returns the model's response untouched", async () => {
    const original = { id: "x", model: "m", choices: [], extra: "kept" };
    const openai = { chat: { completions: { create: async (_args?: Record<string, unknown>) => original } } };

    let seen: unknown;
    await run(async () => {
      seen = await watchOpenAI(openai).chat.completions.create({ model: "m" });
    });

    expect(seen).toBe(original);
  });

  it("records a FAILED call and rethrows", async () => {
    // "The agent did nothing because the provider was down" is a materially
    // different story from "the agent chose to do nothing", and only one of
    // them is in the record if failures are dropped.
    const openai = {
      chat: { completions: { create: async (_args?: Record<string, unknown>) => { throw new Error("503"); } } },
    };

    const steps: Array<Record<string, unknown>> = [];
    const { fetchMock, steps: captured } = recorder();
    const client = new Rotascale({ baseUrl: "https://rotagrant.test",fetch: fetchMock, logger: silent });
    const trajectory = await client.openTrajectory({ agentId: "agt_1" });

    await witness(trajectory, async () => {
      await expect(
        watchOpenAI(openai).chat.completions.create({ model: "m" }),
      ).rejects.toThrow("503");
    });
    steps.push(...captured);

    const p = steps.at(-1)!.payload as any;
    expect(p.failed).toBe(true);
    expect(p.error).toContain("503");
  });

  it("works with no trajectory in scope", async () => {
    // An agent that has not opened a trajectory, or whose capture failed open,
    // must still be able to call its model.
    const openai = { chat: { completions: { create: async (_args?: Record<string, unknown>) => ({ model: "m" }) } } };
    await expect(
      watchOpenAI(openai).chat.completions.create({ model: "m" }),
    ).resolves.toBeDefined();
  });
});

// --- it is a drop-in --------------------------------------------------------

describe("the wrapper is transparent", () => {
  it("leaves everything except completions alone", async () => {
    // subhadipmitra@: A Proxy rather than a rebuilt object, so a provider
    // extension this file has never heard of keeps working. Rebuilding would
    // silently drop whatever was not enumerated, which is every future API.
    const openai = {
      chat: { completions: { create: async (_args?: Record<string, unknown>) => ({}) }, other: "kept" },
      embeddings: { create: async (_args?: Record<string, unknown>) => "embedding" },
      apiKey: "sk-test",
    };

    const watched = watchOpenAI(openai as never) as any;
    expect(watched.apiKey).toBe("sk-test");
    expect(await watched.embeddings.create()).toBe("embedding");
    expect(watched.chat.other).toBe("kept");
  });
});

// --- content is not captured by accident ------------------------------------

describe("content capture is opt-in", () => {
  it("records no prompt or response text by default", async () => {
    const openai = {
      chat: { completions: { create: async (_args?: Record<string, unknown>) => ({
        model: "m", choices: [{ message: { content: "SECRET RESPONSE" } }],
      }) } },
    };

    const steps = await run(async () => {
      await watchOpenAI(openai).chat.completions.create({
        model: "m", messages: [{ role: "user", content: "SECRET PROMPT" }],
      });
    });

    const p = JSON.stringify(steps.at(-1)!.payload);
    // A middleware that captured prompts because somebody imported it would put
    // customer data in the evidence store as a side effect of an import.
    expect(p).not.toContain("SECRET PROMPT");
    expect(p).not.toContain("SECRET RESPONSE");
  });

  it("captures and truncates when asked", async () => {
    const long = "x".repeat(5_000);
    const openai = {
      chat: { completions: { create: async (_args?: Record<string, unknown>) => ({
        model: "m", choices: [{ message: { content: long } }],
      }) } },
    };

    const steps = await run(async () => {
      await watchOpenAI(openai, { captureContent: true, limit: 100 })
        .chat.completions.create({ model: "m", messages: [] });
    });

    const p = steps.at(-1)!.payload as any;
    expect(String(p.response)).toContain("[truncated]");
    expect(String(p.response).length).toBeLessThan(200);
  });

  it("records tool NAMES without their arguments", async () => {
    const openai = {
      chat: { completions: { create: async (_args?: Record<string, unknown>) => ({
        model: "m",
        choices: [{ message: { tool_calls: [
          { function: { name: "issue_refund", arguments: '{"iban":"SECRET"}' } },
        ] } }],
      }) } },
    };

    const steps = await run(async () => {
      await watchOpenAI(openai).chat.completions.create({ model: "m" });
    });

    const p = steps.at(-1)!.payload as any;
    expect(p.tool_calls).toEqual(["issue_refund"]);
    expect(JSON.stringify(p)).not.toContain("SECRET");
  });
});
