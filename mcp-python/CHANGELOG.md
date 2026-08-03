# Changelog

## 0.1.1 — 2026-08-04

A tool call refused because **no grant covers it** is now recorded as a
decision rather than refused locally and written nowhere.

It is the strongest refusal this product makes — the proxy reads the tool name
off the wire and answers instead of forwarding, so the tool never runs and the
agent gets no vote — and it was absent from every count of refusals, including
the assurance file. A deployment enforcing hard at the proxy looked, in its own
evidence, like one refusing nothing.

Needs `rotascale>=0.3.2`.

## Unreleased

### `rotascale_mcp.guard` — the server side

`CapabilityGuard` verifies a capability token before an MCP tool runs, offline,
against a cached public key.

The proxy is a real enforcement point with one gap: it is **bypassable by not
using that tool**. An agent talking to your MCP server directly, or a second
agent nobody routed through the proxy, never meets it. The guard lives in the
server, so there is nothing to route around.

```python
guard = CapabilityGuard(
    audience="mcp://contracts.acme.internal",
    amounts={"settle_payment": lambda args: args["amount_minor"]},
)

async def call_tool(name, arguments):
    arguments = guard.check(name, arguments)   # raises Refused, or returns
    return await tools[name](**arguments)      # cleaned of the token
```

Everything is protected by default — a server that lists its protected tools
will forget one. A protected tool with no way to compare what it was asked for
is **refused**, not allowed: the token's existence becoming the whole
authorisation is a real position, and somebody has to take it deliberately by
naming the tool in `quantityless`.

`aud` names the server, not the tool, so the token's `act.tool` is checked
against the tool being called — otherwise a token for a cheap read would
authorise an expensive write on the same server.

The token travels in `_meta` (the protocol's own slot) or in a reserved
argument key for clients that cannot set it, and is stripped before the tool
sees it. A tool whose signature grew a parameter because of us is one we broke.

Install with `pip install rotascale-mcp[guard]` — the common install is the
proxy, which sits on the agent side and verifies nothing.

## 0.1.0 — 2026-08-01

First release. Two surfaces for governing agents that speak MCP, including
agents whose code you cannot touch.

### `rotascale-mcp` — governance as MCP tools

`open_trajectory`, `authorize_action`, `witness_step`, `check_authority`,
`close_trajectory`. Any MCP host gains them without integrating an SDK.

`authorize_action` returns an **outcome, not a boolean** — `allow`, `deny`,
`exhausted`, `gated`, `review_sync`, `review_async` — each with `guidance`
written for a model to act on. A boolean would collapse them, and an agent that
cannot tell `exhausted` from `gated` will retry: useless for the first, a
security problem for the second.

**Opt-in, and therefore advisory.** An agent that never calls
`authorize_action` is not governed by it.

### `rotascale-mcp-proxy` — the surface an agent cannot opt out of

Sits between the agent and its real MCP server. Every tool call passes through
and is authorised before it is forwarded; a refusal never reaches the tool.
Tool manifests are reported and a poisoned description taints the trajectory, so
the next privileged action refuses.

**This is the control.** The server surface is not.

### Notes

- forwards ungoverned traffic byte-for-byte
- never wedges the relay: if governance fails, it logs and forwards
- `ROTASCALE_MCP_REQUIRE_GRANT=1` to refuse tools no grant covers
- argument *names* are recorded; their values never leave the customer
