# Changelog

## 0.3.0 — 2026-08-02

**A resource can now refuse.** `rotascale.capability` verifies a capability
token at the thing being protected — a payments service, an internal API — with
no Rotascale client, no API key and no callback. Fetch the JWK set once, verify
offline afterwards: our availability is not yours.

Everything else in this SDK is *advice*. On the instrumented path the code that
asks and the code that acts are the same code, so an agent can simply not ask.
A token is checked by somebody else, and an agent that cannot produce a valid
one cannot act.

```python
from rotascale.capability import verify, Refused

claim = verify(token, audience="https://payments.acme.internal", jwks=KEYS)
if claim.amount_minor < requested:
    raise Forbidden("authorised for less than requested")
```

That last comparison is not optional. Verifying a signature proves *some*
authority existed; comparing the claim against what was actually requested
proves *this* one did.

An optional extra — `pip install rotascale[capability]` — because the
overwhelming majority of installs are agents, which neither mint nor verify,
and the base package still carries no cryptography stack.

**`Decision.capability`** carries the credential for that action when the grant
names a resource. `None` otherwise, which is most grants.

**`SeenTokens`** refuses a replayed token without breaking an honest retry. The
action succeeded, the response was lost, the client retries — refusing that
turns one lost packet into a failed payment, so it replays the recorded answer
instead. A concurrent second presentation, where there is no answer yet, is
refused.

**Agent identity includes the environment.** `(workspace, environment, slug)`,
taken from the API key. The same slug under a `rota_test_` key is a different
governed subject from the one under `rota_live_`, and cannot exercise its
grants. `str(agent)` shows the environment only when it is not production —
`test/refund-assistant` in a production log is the thing worth seeing.

**Provenance reports what changed.** `report_provenance()` no longer discards
the response: it logs the version move, and warns when grants were attested
against the configuration being replaced, or when a version string did not move
while the content hash did.

## 0.2.2 — 2026-08-02

**Ten frameworks, one line each.** Gemini, Bedrock, LangChain, LangGraph, ADK,
CrewAI, Strands and AutoGen join OpenAI-compatible and Anthropic. Still no
provider dependency: verified in a clean environment that none of the ten
libraries is installed alongside.

**ADK can refuse.** `watch_adk(agent, grant=GRANT)` authorises tool calls
before they run, and a refusal cancels the call — ADK's before-callback can
short-circuit, so this is a real enforcement point rather than a record of one.
Without `grant=` it observes like the rest. Only ADK and
`rotascale-mcp-proxy` are controls; the others witness.

**LangChain and LangGraph are callback handlers.** Build them *inside* the
witness block — LangChain fires callbacks from a thread pool, and the active
trajectory is a ContextVar that does not cross a thread boundary. A handler
built outside says so loudly rather than silently recording nothing.

LangGraph additionally records the node traversal with visit counts, so a loop
reads as a loop.

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
