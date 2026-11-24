# rotascale-sdks

Client libraries for [Rotascale](https://rotascale.com): agent governance.

| Package | Published as | Install |
|---|---|---|
| [`python/`](python) | [`rotascale`](https://pypi.org/project/rotascale/) on PyPI | `pip install rotascale` |
| [`mcp-python/`](mcp-python) | [`rotascale-mcp`](https://pypi.org/project/rotascale-mcp/) on PyPI | `pip install rotascale-mcp` |
| [`typescript/`](typescript) | [`@rotascale/sdk`](https://www.npmjs.com/package/@rotascale/sdk) on npm | `npm install @rotascale/sdk` |

[`npm-name/`](npm-name) holds the unscoped npm name `rotascale` and points at
`@rotascale/sdk`. It contains no code.

Every package is licensed under [Apache 2.0](LICENSE).

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

See [the documentation](https://docs.rotascale.com) for what the primitives mean, and the
[quickstart](https://docs.rotascale.com/quickstart) for a first governed call.

## Questions and defects

[Open an issue here](https://github.com/rotascale/rotascale-sdks/issues). The platform's own
repository is private, so an issue filed against a client belongs in this one.
