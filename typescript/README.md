# @rotascale/sdk

Govern the action, not the model.

```ts
import { Rotascale } from "@rotascale/sdk";

const rotascale = new Rotascale();               // ROTASCALE_API_KEY, ROTASCALE_API_URL

const run = await rotascale.openTrajectory({ agentId, externalRef: "TICKET-4471" });
await run?.step({ kind: "retrieval", sourceRef: "upload:DOC-9002" });

const decision = await rotascale.authorize({
  grantId,
  scope: { tools: ["issue_refund"] },
  amountMinor: 4_500,
  currency: "EUR",
  trajectoryId: run?.id,
});

await run?.close({ outcome: { refunded: true } });
```

## The two things worth knowing

**Capture never throws; enforcement always can.** Losing evidence is bad, taking
down your agent is worse, and an agent acting because the governance layer was
unreachable is worst. So `openTrajectory` returns `null` on failure and `step`
swallows, while `authorize` throws `EnforcementUnavailable` if Rotascale cannot
be reached.

`failOpenEnforcement: true` inverts that last one. It is off by default and logs
every time it is used — a caller may decide availability matters more than
governance for their workload, but nobody should arrive there by accident. It
covers an unreachable control plane only: a refusal from a reachable server
still throws.

**`allowed` is not the whole answer.** A grant in `observe` returns
`allowed: true` for everything.

```ts
if (!decision.enforcing) {
  // This grant is measuring, not refusing.
}
if (decision.suppressed) {
  // The policy refused and the mode let it through. In production, the
  // control you believe is running is not.
}
```

Read `enforcing` rather than comparing `enforcementMode` to a string: the set of
modes will grow, and `mode === "enforce"` silently treats a new non-enforcing
rung as enforcement.

## Your own models, on your own hardware

Rotascale governs **the action**, not the model — so which model produced the
request is not something it needs to know, and nothing here talks to a model
provider.

The Python middleware is deliberately named `openai_compat` rather than
`openai`, and imports no provider library: it duck-types anything exposing
`chat.completions.create`. That covers Ollama, vLLM, LM Studio, LiteLLM, Azure
OpenAI, Together, Groq — every service that copied the shape.

This is not aspirational. The Rotascale demo runs entirely on a self-hosted
`llama3.2:3b` served by Ollama at `http://ollama:11434/v1`; every governed
decision it shows was made about an agent talking to a local model. No request
leaves the deployment.

For an on-prem installation the same holds for the control plane: the API,
Postgres, Keycloak and the Cedar services all run in your own environment, and
`baseUrl` points at them.

```ts
const rotascale = new Rotascale({ baseUrl: "https://rotascale.internal" });
```

```ts
import { watchOpenAI, witness } from "@rotascale/sdk";
import OpenAI from "openai";

const llm = watchOpenAI(new OpenAI({ baseURL: "http://localhost:11434/v1" }));

await witness(run, async () => {
  await llm.chat.completions.create({ model: "llama3.2:3b", messages });
  // the call is on the trajectory; no other change to the agent
});
```

`watchAnthropic` is the same shape for `messages.create`. Both normalise usage
to `prompt_tokens` / `completion_tokens`, so a fleet's numbers can be summed
without knowing which provider served which call.

Content capture is **off** by default and truncated when on. Tool **names** are
recorded; their arguments are not.

## Refusals

The exception type tells you the remedy.

| Type | Means | Remedy |
|---|---|---|
| `Blocked` | out of scope, expired, revoked | change the grant |
| `Exhausted` | budget or call count spent | raise the budget |
| `Gated` | context tainted, grant requires it clean | sanitiser or approval |
| `ReviewRequired` | a person must decide | park the action; the queue item exists |
| `RequestRefused` | the server answered and the request was wrong | fix the call |
| `EnforcementUnavailable` | Rotascale could not be reached | your call |

## Development

```sh
npm install
npm test          # unit + contract
npm run typecheck
npm run build
```

`test/contract.test.ts` validates the request body against the console's
committed `openapi.json`. The unit tests mock the server, so they check the
shape this SDK believes in; the contract test checks the shape the API accepts.
Both are needed — an early version passed every unit test while sending a body
the API rejected.
