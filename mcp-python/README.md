# rotascale-mcp

Govern any agent that speaks the Model Context Protocol — including agents whose
code you cannot touch.

```bash
pip install rotascale-mcp
```

## Two surfaces, and the difference is the whole point

| | `rotascale-mcp` | `rotascale-mcp-proxy` |
|---|---|---|
| How it is reached | the agent decides to call a governance tool | every tool call passes through it |
| Can the agent avoid it | **yes** | **no** |
| Enforcement | advisory | in the tool path |

**Only the proxy is a control.** The server surface is genuinely useful — real
evidence, and a real gate for an agent that cooperates — but an agent that never
calls `authorize_action` is not governed by it. We would rather say that plainly
than let you find out during an incident.

### Two settings decide how much the proxy enforces

Out of the box the proxy **records** a tool call no grant covers and forwards
it. That keeps an unconfigured install working rather than breaking it, and the
call is visible in the console as ungoverned — but it is observation, not
enforcement. To refuse instead:

```
ROTASCALE_MCP_REQUIRE_GRANT=1
```

If a grant carries a **spending budget**, the proxy has to know how much a call
would spend, and it will not read your tool arguments to guess. Name the field:

```
ROTASCALE_MCP_AMOUNT_FIELDS=issue_refund:amount_minor,transfer_funds:value
ROTASCALE_MCP_AMOUNT_FIELD=amount_minor      # fallback for any other tool
```

Only that one argument is sent. Everything else stays argument *names* and never
values, as before.

**Most agents need none of this.** A tool with no money in it — `send_email`,
`read_file` — is governed by scope and count budgets, which never needed an
amount. The trigger is the grant, not the tool.

If a grant *does* carry a spending budget and you have declared nothing, the
proxy **refuses** calls under it and says which variable to set. It used to
authorize them at zero, which meant the budget could never be exhausted and an
agent could move any sum one call at a time — with the ledger recording
`allow / authorised` throughout. Failing closed and loudly is the correct
behaviour for an enforcement point; see `#152`.

Once you have named even one money tool on a grant, silence about the others is
taken as a statement that they carry no money, so a harmless sibling tool under
the same grant keeps working.

## Rotascale as an MCP server

Adds governance tools to any MCP host. The agent chooses when to call them.

```json
{
  "mcpServers": {
    "rotascale": {
      "command": "rotascale-mcp",
      "env": {
        "ROTASCALE_URL": "https://rotascale.acme.internal",
        "ROTASCALE_API_KEY": "rota_live_…"
      }
    }
  }
}
```

| Tool | When the agent calls it |
|---|---|
| `open_trajectory` | once, at the start of a task |
| `authorize_action` | **before** any consequential action — moving money, changing a record, contacting a person |
| `witness_step` | as it reads and acts; `kind="retrieval"` is what carries taint |
| `check_authority` | to see what it may do and what budget remains |
| `close_trajectory` | when the task ends, success or failure |

### `authorize_action` returns an outcome, not a boolean

Six outcomes, each with a different remedy, and a `guidance` string written for
a model to act on:

| Outcome | What it means |
|---|---|
| `allow` | proceed |
| `deny` | outside the granted authority — do not retry, do not route around |
| `exhausted` | budget or call allowance spent — retrying cannot help |
| `gated` | the context is tainted and this authority needs a clean one |
| `review_sync` | a human must decide first |
| `review_async` | proceed, but it is queued for review |

A boolean would collapse these, and an agent that cannot tell `exhausted` from
`gated` will do the wrong thing about both — usually retrying, which is useless
for the first and a security problem for the second.

## Transport

stdio by default, because that is how MCP hosts launch a local server. Logging
goes to stderr, since stdout *is* the protocol channel.

```bash
ROTASCALE_MCP_TRANSPORT=streamable-http rotascale-mcp
```

## Why a separate package

`pip install rotascale` must never carry an MCP dependency, and the MCP spec
revises on its own schedule. Pinning them together would force pointless
releases of one to keep up with the other.

## Tracking MCP servers you already use

Separate from this package: the `rotascale` SDK's `watch_mcp` wraps an MCP
client session and reports each server's tool manifest, so a tool whose
*description* changes is caught — including between sessions. A description is
an instruction the model reads, so rewriting one changes what your agent does
without changing a line of your code.

```python
from rotascale.middleware import watch_mcp

session = watch_mcp(session, server="filesystem", transport="stdio")
```
