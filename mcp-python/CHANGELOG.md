# Changelog

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
