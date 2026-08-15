import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it, vi } from "vitest";

import { Rotascale } from "../src/index.js";

/**
 * The request this client sends, checked against the API's real schema.
 *
 * subhadipmitra@: `posture.test.ts` mocks the server, so it validates the
 * shape this SDK BELIEVES IN rather than the shape the API accepts. Every one
 * of those tests passed while `authorize` sent `amount_minor: null` — which
 * `AuthorizeIn` rejects, because the field is `int = Field(default=0, ge=0)`
 * and an explicit null is not an int.
 *
 * So every authorize call without an amount was a 422, and the SDK reported it
 * as `EnforcementUnavailable`. Under `failOpenEnforcement` that meant the
 * action was ALLOWED. Two bugs stacked: the wrong body, and a refusal read as
 * an outage.
 *
 * Found by running against the live API. This closes the gap without needing
 * one: `openapi.json` is committed in the console repo, so the schema is right
 * here and a body that would 422 fails in CI instead.
 */

const HERE = dirname(fileURLToPath(import.meta.url));

/**
 * The VENDORED fragment, which is always present.
 *
 * subhadipmitra@: Vendored rather than fetched, and the reason is that a
 * cross-repo checkout of a private console needs a credential on a runner —
 * so the alternative was a contract test that skips in CI, which reads as
 * coverage and is not. That is the `#149` shape and it is not worth repeating
 * for the sake of avoiding one committed file.
 *
 * The trade is staleness, and `test/contract.test.ts` closes it below: when
 * the console IS checked out beside this repo the two are compared, so anybody
 * with both finds out immediately.
 */
const VENDORED = join(HERE, "..", "schema", "api.json");

/** The real thing, when this machine has it. */
const UPSTREAM = process.env.ROTASCALE_OPENAPI
  ?? join(HERE, "..", "..", "..", "rotascale-console", "api", "openapi.json");

function schema(): Record<string, unknown> {
  return JSON.parse(readFileSync(VENDORED, "utf8"));
}

function upstream(): Record<string, unknown> | null {
  try {
    return JSON.parse(readFileSync(UPSTREAM, "utf8"));
  } catch {
    return null;
  }
}

function required(model: string): { props: Record<string, any>; required: string[] } {
  const s = (schema() as any).components.schemas[model];
  return { props: s?.properties ?? {}, required: s?.required ?? [] };
}

/** Does this JSON value satisfy the OpenAPI type for that field? */
function accepts(spec: any, value: unknown): boolean {
  if (!spec) return true;
  // `anyOf` with a null branch is how `str | None` is emitted.
  if (spec.anyOf) return spec.anyOf.some((s: any) => accepts(s, value));
  if (spec.type === "null") return value === null;
  if (value === null) return false;
  if (spec.type === "integer") return Number.isInteger(value);
  if (spec.type === "string") return typeof value === "string";
  if (spec.type === "object") return typeof value === "object";
  if (spec.type === "array") return Array.isArray(value);
  if (spec.type === "boolean") return typeof value === "boolean";
  return true;
}

function bodyOf(mock: ReturnType<typeof vi.fn>, call = 0): Record<string, unknown> {
  return JSON.parse(String((mock.mock.calls[call] as any)[1].body));
}

function ok(payload: unknown) {
  return vi.fn<typeof globalThis.fetch>(
    async () => new Response(JSON.stringify(payload), { status: 200 }));
}

describe("the authorize body matches AuthorizeIn", () => {
  it("sends nothing the schema rejects", async () => {
    const spec = required("AuthorizeIn");

    const fetchMock = ok({ outcome: "allow", allowed: true, reason: "ok" });
    const client = new Rotascale({ baseUrl: "https://rotagrant.test",fetch: fetchMock, logger: { error() {}, warn() {} } });

    // Deliberately the MINIMAL call: no amount, no currency, no trajectory.
    // That is the one that was broken, and the one most callers make first.
    await client.authorize({ grantId: "grt_1", scope: { tools: ["x"] } });

    const body = bodyOf(fetchMock);
    const rejected = Object.entries(body).filter(
      ([field, value]) => !accepts(spec.props[field], value));

    expect(rejected, `these fields would 422: ${JSON.stringify(rejected)}`)
      .toEqual([]);
  });

  it("sends amount_minor as an integer, never null", async () => {
    // The exact defect. `AuthorizeIn.amount_minor` is a non-optional int with
    // a default, so null is a 422 and an absent field is fine.
    const fetchMock = ok({ outcome: "allow", allowed: true, reason: "ok" });
    const client = new Rotascale({ baseUrl: "https://rotagrant.test",fetch: fetchMock, logger: { error() {}, warn() {} } });

    await client.authorize({ grantId: "grt_1" });

    expect(bodyOf(fetchMock).amount_minor).toBe(0);
  });

  it("names no field the schema has never heard of", async () => {
    const spec = required("AuthorizeIn");

    const fetchMock = ok({ outcome: "allow", allowed: true, reason: "ok" });
    const client = new Rotascale({ baseUrl: "https://rotagrant.test",fetch: fetchMock, logger: { error() {}, warn() {} } });
    await client.authorize({
      grantId: "grt_1", amountMinor: 100, currency: "EUR",
      trajectoryId: "trj_1", incumbentDecision: "allow",
    });

    const unknown = Object.keys(bodyOf(fetchMock))
      .filter((f) => !(f in spec.props));
    expect(unknown, `fields the API does not declare: ${unknown}`).toEqual([]);
  });
});

describe("the vendored schema is current", () => {
  it("matches the console's committed spec, when it is checked out here", () => {
    // subhadipmitra@: The whole cost of vendoring, paid here. Anybody with
    // both repos — which is anybody changing the API — finds out on the next
    // `npm test` rather than when a customer's call starts 422-ing.
    const real = upstream();
    if (real === null) return;   // console absent; CI has the vendored copy.

    const ours = (schema() as any).components.schemas;
    const theirs = (real as any).components.schemas;

    for (const model of Object.keys(ours)) {
      expect(ours[model], `${model} has drifted — run \`npm run schema:sync\``)
        .toEqual(theirs[model]);
    }
  });
});

describe("the decision it reads matches AuthorizeOut", () => {
  it("reads only fields the API declares", () => {
    const doc = schema();
    const declared = new Set(Object.keys(
      (doc as any).components?.schemas?.AuthorizeOut?.properties ?? {}));

    // Every wire name `Decision` maps. A rename server-side silently becomes
    // `null` in this client rather than an error, so it is checked here.
    for (const field of [
      "outcome", "allowed", "reason", "grant_id", "ledger_id",
      "remaining_amount_minor", "remaining_count", "findings",
      "policy_outcome", "enforcement_mode", "capability",
      "capability_expires_at",
    ]) {
      expect(declared, `AuthorizeOut has no ${field}`).toContain(field);
    }
  });
});
