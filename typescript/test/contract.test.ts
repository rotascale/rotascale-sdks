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
const OPENAPI = join(HERE, "..", "..", "..", "rotascale-console", "api", "openapi.json");

function schema(): Record<string, unknown> | null {
  try {
    return JSON.parse(readFileSync(OPENAPI, "utf8"));
  } catch {
    // The console repo is not always checked out beside this one. Skipping is
    // honest; pretending to have validated is not.
    return null;
  }
}

function required(model: string): { props: Record<string, any>; required: string[] } | null {
  const doc = schema();
  if (!doc) return null;
  const s = (doc as any).components?.schemas?.[model];
  if (!s) return null;
  return { props: s.properties ?? {}, required: s.required ?? [] };
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
    if (!spec) return; // console repo absent — see `schema()`.

    const fetchMock = ok({ outcome: "allow", allowed: true, reason: "ok" });
    const client = new Rotascale({ fetch: fetchMock, logger: { error() {}, warn() {} } });

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
    const client = new Rotascale({ fetch: fetchMock, logger: { error() {}, warn() {} } });

    await client.authorize({ grantId: "grt_1" });

    expect(bodyOf(fetchMock).amount_minor).toBe(0);
  });

  it("names no field the schema has never heard of", async () => {
    const spec = required("AuthorizeIn");
    if (!spec) return;

    const fetchMock = ok({ outcome: "allow", allowed: true, reason: "ok" });
    const client = new Rotascale({ fetch: fetchMock, logger: { error() {}, warn() {} } });
    await client.authorize({
      grantId: "grt_1", amountMinor: 100, currency: "EUR",
      trajectoryId: "trj_1", incumbentDecision: "allow",
    });

    const unknown = Object.keys(bodyOf(fetchMock))
      .filter((f) => !(f in spec.props));
    expect(unknown, `fields the API does not declare: ${unknown}`).toEqual([]);
  });
});

describe("the decision it reads matches DecisionOut", () => {
  it("reads only fields the API declares", () => {
    const doc = schema();
    if (!doc) return;
    const declared = new Set(Object.keys(
      (doc as any).components?.schemas?.DecisionOut?.properties ?? {}));
    if (declared.size === 0) return;

    // Every wire name `Decision` maps. A rename server-side silently becomes
    // `null` in this client rather than an error, so it is checked here.
    for (const field of [
      "outcome", "allowed", "reason", "grant_id", "ledger_id",
      "remaining_amount_minor", "remaining_count", "findings",
      "policy_outcome", "enforcement_mode", "capability",
      "capability_expires_at",
    ]) {
      expect(declared, `DecisionOut has no ${field}`).toContain(field);
    }
  });
});
