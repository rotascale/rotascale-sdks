/**
 * Refresh the vendored schema fragment from the console repo.
 *
 * subhadipmitra@: Vendored rather than fetched, because the contract test must
 * run in CI and the console is a separate private repository — a cross-repo
 * checkout needs a credential on a runner, and the alternative was a test that
 * skips. A skipped contract test reads as coverage and is not.
 *
 * The copy can go stale, which is the trade. `contract.test.ts` compares it
 * against the real file whenever the console is checked out beside this one,
 * so anybody with both repos finds out immediately.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCE = process.env.ROTASCALE_OPENAPI
  ?? join(HERE, "..", "..", "..", "rotascale-console", "api", "openapi.json");
const TARGET = join(HERE, "..", "schema", "api.json");

// Only what the contract test reads. A whole-spec copy would churn on every
// unrelated endpoint and nobody would read the diff.
const WANTED = ["AuthorizeIn", "AuthorizeOut"];

const doc = JSON.parse(readFileSync(SOURCE, "utf8"));
const schemas = Object.fromEntries(
  WANTED.filter((k) => k in doc.components.schemas)
        .map((k) => [k, doc.components.schemas[k]]));

const missing = WANTED.filter((k) => !(k in schemas));
if (missing.length) {
  console.error(`these models are not in the spec: ${missing.join(", ")}`);
  process.exit(1);
}

writeFileSync(TARGET, JSON.stringify({
  _note: "VENDORED from rotascale-console/api/openapi.json. Not hand-edited. "
    + "Refresh with `npm run schema:sync` when the API changes; "
    + "test/contract.test.ts checks this against the real file whenever the "
    + "console repo is checked out beside this one.",
  components: { schemas },
}, null, 2) + "\n");

console.log(`synced ${WANTED.join(", ")} from ${SOURCE}`);
