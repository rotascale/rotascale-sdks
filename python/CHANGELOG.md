# Changelog

## 0.2.1 — 2026-08-02

**Provenance reports itself.** The middlewares now tell Rotascale which model
actually **served** a call, the first time they see one. The served identity,
not the requested one — ask for `gpt-4o` and a dated build answers, and only
what ran is evidence of what ran.

Reported once per `(agent, model)`, not once per call: this is an HTTP request
on your agent's critical path. A genuine model switch still reports.

`rs.report_provenance(agent, model=…, prompt_version=…, tools=…)` is public for
anything the middleware cannot see.

Never raises. A provider middleware that broke your agent because our inventory
was unreachable would be indefensible.

Requires a Rotascale server with `#59`.

First release published from CI via trusted publishing rather than from a
laptop.

## 0.2.0 — 2026-08-01

**This is a different product from 0.1.1 under the same name.** 0.1.1 was the
SDK for the Trust Intelligence Platform, which has been retired. Nothing in this
release is source-compatible with it. PyPI never allows a version to be
replaced, so 0.2.0 supersedes rather than overwrites.

### The SDK now

- **Authenticate with a workspace API key.** `Rotascale(api_key="rota_live_…")`
  or `ROTASCALE_API_KEY`. An agent inside a customer runtime cannot complete an
  OIDC flow, so this is the credential that actually gets used. Missing or
  malformed credentials fail at construction rather than on the first call.
- **Agents name themselves.** `rs.agent("refund-assistant")` creates on first
  sight and is idempotent after, so it is safe on every process start. No more
  copying an opaque id out of a console into your source.
- **`agent.governed`** says whether the agent holds authority yet. An agent that
  has just been discovered records evidence but enforces nothing, and an
  integration that cannot tell the difference reports success while the customer
  believes otherwise.
- **MCP manifests are reported**, so a tool description that changes *between*
  sessions is caught — not only one that changes while an agent is connected.
  Description and schema are hashed separately: a moved schema is a version
  bump, a moved description while the schema holds is the signature of tool
  poisoning.
- Apache-2.0, `py.typed`, and no provider dependency of any kind. Middlewares
  are duck-typed, so installing this never touches your lockfile.

### Contract

Capture never raises; enforcement raises by default. Losing evidence is
survivable, losing the authority check is not.
