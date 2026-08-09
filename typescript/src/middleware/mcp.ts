/**
 * MCP middleware — and the tool-poisoning detector.
 *
 *     const session = watchMcp(rawSession);
 *
 * Wraps an MCP client session so tool discovery and tool calls land on the
 * trajectory. Duck-typed, like every middleware here: it imports no MCP SDK and
 * wraps anything exposing `listTools` / `callTool`.
 *
 * ## The manifest digest is the point
 *
 * A tool's *description* is an instruction to a model. A malicious or
 * compromised MCP server can rewrite one mid-session — the agent re-reads the
 * tool list, the description now says something new, and the model follows it.
 * Nothing about the transcript looks wrong, because the attack lives in
 * metadata nobody records.
 *
 * A description or schema change between two `listTools()` calls in one session
 * raises a finding AND taints the context, so a grant requiring a clean context
 * stops the agent before it can act on the injection.
 *
 * ## Byte-identical to the Python SDK, deliberately
 *
 * subhadipmitra@: The digest has to match `rotascale.middleware.mcp_api`
 * exactly. A fleet running both SDKs against the same MCP server would
 * otherwise report drift on every handover between them — a tool-poisoning
 * alert with no poisoning, which is the fastest way to get a detector switched
 * off.
 *
 * That is harder than it looks, and `JSON.stringify` gets it wrong twice:
 *
 *   - Python's `json.dumps` defaults to `", "` and `": "` separators, so it
 *     emits `{"a": 1}` where `JSON.stringify` emits `{"a":1}`.
 *   - Python defaults to `ensure_ascii=True`, so `é` becomes `é`. A tool
 *     description containing an em dash — which is most of them — would hash
 *     differently in the two SDKs.
 *
 * `pythonJson` below reproduces both. `test/mcp.test.ts` asserts the digests
 * against values computed by the real Python implementation rather than by this
 * one, because a test that generates its own expectation would agree with
 * whatever it got wrong.
 */

import { record } from "./common.js";

/** MCP SDKs and plain objects both turn up in the wild. */
function attr(target: unknown, name: string): unknown {
  if (target && typeof target === "object") {
    return (target as Record<string, unknown>)[name];
  }
  return undefined;
}

function toolsFrom(result: unknown): unknown[] {
  const tools = attr(result, "tools");
  if (Array.isArray(tools)) return tools;
  return Array.isArray(result) ? result : [];
}

/** A string escaped the way Python's `json.dumps` escapes it. */
function pythonString(value: string): string {
  // JSON.stringify handles quotes, backslashes and control characters
  // identically; only the non-ASCII half differs.
  return JSON.stringify(value).replace(
    // eslint-disable-next-line no-control-regex
    /[\u0080-\uffff]/g,
    (character) =>
      `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

/** `json.dumps(value, sort_keys=True)`, byte for byte. */
export function pythonJson(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : String(value);
  }
  if (typeof value === "string") return pythonString(value);
  if (Array.isArray(value)) {
    return `[${value.map(pythonJson).join(", ")}]`;
  }
  if (typeof value === "object") {
    const entries = Object.keys(value as Record<string, unknown>)
      .sort()
      .map((key) =>
        `${pythonString(key)}: ${pythonJson((value as Record<string, unknown>)[key])}`);
    return `{${entries.join(", ")}}`;
  }
  return pythonString(String(value));
}

async function sha256(input: string): Promise<string> {
  const bytes = new TextEncoder().encode(input);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export type ManifestDigest = {
  combined: string;
  perTool: Record<string, string>;
};

/**
 * SHA-256 over each tool's (name, description, input schema).
 *
 * The description and schema are IN the digest deliberately — they are the
 * whole attack surface. Hashing names alone would miss the entire
 * tool-poisoning class, because the tool list looks identical while the
 * instructions attached to it have changed underneath.
 */
export async function manifestDigest(tools: unknown[]): Promise<ManifestDigest> {
  const perTool: Record<string, string> = {};
  for (const tool of tools) {
    const name = String(attr(tool, "name") ?? "");
    const description = String(attr(tool, "description") ?? "");
    const schema = attr(tool, "inputSchema") ?? attr(tool, "input_schema") ?? {};
    // \u001f between fields, \u001e between tools — matching the Python SDK.
    // A separator that could appear inside a description would let one tool's
    // text impersonate a field boundary.
    perTool[name] = await sha256(
      `${name}\u001f${description}\u001f${pythonJson(schema)}`,
    );
  }
  const combined = await sha256(
    Object.keys(perTool)
      .sort()
      .map((name) => `${name}:${perTool[name]}`)
      .join("\u001e"),
  );
  return { combined, perTool };
}

type McpSession = {
  listTools(...args: unknown[]): Promise<unknown>;
  callTool(...args: unknown[]): Promise<unknown>;
};

export type WatchMcpOptions = {
  /** Names this server in every finding. Without it a finding says a manifest
   *  changed but not whose, which is unactionable in a fleet. */
  server?: string;
};

/**
 * Wrap an MCP client session. Capture only — it never blocks a call, and never
 * throws into the agent.
 */
export function watchMcp<T extends McpSession>(
  session: T, options: WatchMcpOptions = {},
): T {
  const server = options.server ?? "mcp";
  let digest: string | null = null;
  let perTool: Record<string, string> = {};

  async function checkManifest(tools: unknown[]): Promise<void> {
    const current = await manifestDigest(tools);

    if (digest === null) {
      digest = current.combined;
      perTool = current.perTool;
      await record("mcp_manifest", {
        mcp_server: server,
        manifest_digest: current.combined,
        tool_count: tools.length,
        // Names only. A description is the injection surface, so shipping it
        // wholesale to the platform would move the payload rather than
        // detect it.
        tools: Object.keys(current.perTool),
      });
      return;
    }

    if (current.combined === digest) return;

    const changed = Object.keys(current.perTool).filter(
      (name) => perTool[name] !== undefined
        && perTool[name] !== current.perTool[name]);
    const added = Object.keys(current.perTool).filter((n) => !(n in perTool));
    const removed = Object.keys(perTool).filter((n) => !(n in current.perTool));

    const previous = digest;
    digest = current.combined;
    perTool = current.perTool;

    // subhadipmitra@: Recorded as an UNTRUSTED retrieval, so it taints the
    // context. A finding alone would be a log line somebody reads afterwards;
    // tainting means a grant requiring a clean context refuses the next action,
    // which stops the agent before it acts on the injection.
    await record("retrieval", {
      mcp_server: server,
      finding: "mcp_manifest_changed",
      trusted: false,
      changed_tools: changed,
      added_tools: added,
      removed_tools: removed,
      previous_digest: previous,
      manifest_digest: current.combined,
    });
  }

  const watched = {
    async listTools(...args: unknown[]): Promise<unknown> {
      const result = await session.listTools(...args);
      try {
        await checkManifest(toolsFrom(result));
      } catch {
        // Capture must never break discovery.
      }
      return result;
    },

    async callTool(...args: unknown[]): Promise<unknown> {
      const first = args[0];
      // The MCP SDKs pass either ({name, arguments}) or (name, arguments).
      const name = typeof first === "string"
        ? first
        : String(attr(first, "name") ?? "");
      const started = Date.now();
      try {
        const result = await session.callTool(...args);
        await record("tool_call", {
          mcp_server: server,
          tool: name,
          latency_ms: Date.now() - started,
          // `isError` is a RESULT, not an exception — an MCP tool reports
          // failure in-band, and treating it as success would record a refused
          // or failed action as a completed one.
          failed: attr(result, "isError") === true,
        });
        return result;
      } catch (error) {
        await record("tool_call", {
          mcp_server: server,
          tool: name,
          latency_ms: Date.now() - started,
          failed: true,
          error: String(error).slice(0, 500),
        });
        throw error;
      }
    },
  };

  return new Proxy(session, {
    get(target, property, receiver) {
      if (property === "listTools") return watched.listTools;
      if (property === "callTool") return watched.callTool;
      return Reflect.get(target, property, receiver);
    },
  });
}
