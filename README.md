# rotascale-sdks

Client libraries for [Rotascale](https://github.com/rotascale/rotascale-console) —
agent governance.

| SDK | Status | Package |
|---|---|---|
| [`python/`](python) | in use | `rotascale` |
| `typescript/` | not built — [#63](https://github.com/rotascale/rotascale-console/issues/63) | `@rotascale/sdk` |

## Why this is a separate repository

SDKs are released on their own cadence and pinned by version in a customer's
code. Bundling them with the platform would couple a client release to a server
commit, and would mean anybody reading the client to decide whether to trust it
has to clone the whole platform to do so.

One repository rather than one per language: the integration examples must
behave **identically** across languages, and keeping them apart is how they stop
doing that. Each package still publishes independently.

## The failure posture, which is not negotiable

Every SDK implements the same two rules, and they are opposites on purpose:

- **Capture fails open.** If Rotascale is unreachable, the agent keeps working
  and evidence is dropped with a warning. Governance infrastructure must never
  take production down.
- **Enforcement fails closed.** If Rotascale cannot be reached for an
  authorisation decision, the call raises. An ungoverned action is worse than a
  delayed one.

An SDK that inherits the wrong posture is worse than no SDK, because the agent
looks governed.

## Contract

Both clients speak to the same API and must agree on it. Where a governance fact
is derived — a count, a rate, a status — it is derived on the **server**, so two
clients cannot report different answers from the same data.

See [`docs/concepts/`](https://github.com/rotascale/rotascale-console/tree/main/docs/concepts)
for what the primitives mean, and
[`09-integration.md`](https://github.com/rotascale/rotascale-console/blob/main/docs/rotascale-strategy/09-integration.md)
for how integration is meant to work.
