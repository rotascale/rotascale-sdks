# rotascale — Python SDK

Govern what your agents are allowed to do, and prove what they did.

```bash
pip install rotascale
```

## The whole happy path

```python
from rotascale import Rotascale, Gated

rs = Rotascale("https://rotascale.acme.internal", token=TOKEN)

with rs.witness("refund-agent", ref="TICKET-88123") as t:
    t.retrieval("https://customer-attachment.example/note.pdf")   # untrusted -> taints
    try:
        t.authorize(GRANT, {"tools": ["issue_refund"]}, amount_minor=9_000)
        issue_refund(...)
    except Gated:
        escalate_to_human()                                       # read something untrusted
    t.outcome(decision="escalated")
```

Three lines to record, one to enforce. Anything requiring an agent rewrite or a
framework migration is rejected at design time.

## The contract: capture fails open, enforcement fails closed

**Nothing you call to record can raise.** If Rotascale is unreachable the SDK
logs a warning and your agent keeps working. Losing evidence is bad; taking down
production is worse.

**Everything you call to enforce can raise**, and does by default:

| Exception | Meaning | Remedy |
|---|---|---|
| `Blocked` | out of scope, past a ceiling, expired, revoked | change the grant |
| `Exhausted` | budget or call count spent | raise the budget |
| `Gated` | context is tainted and this grant needs a clean one | human approval or a sanitiser |
| `ReviewRequired` | a human must decide first | park the action |
| `EnforcementUnavailable` | Rotascale unreachable | **fails closed** — an ungoverned action is worse than a delayed one |

The exception type names the remedy, because "refused" alone tells you nothing
about what to do next. Pass `raise_on_refusal=False` to branch on outcomes yourself.

## Middlewares

Duck-typed — none imports the library it wraps, so the SDK never drags a
provider dependency into your lockfile.

```python
from rotascale.middleware import watch_openai, watch_anthropic, watch_mcp

client  = watch_openai(OpenAI())            # or Azure, Together, Groq, vLLM, Ollama…
claude  = watch_anthropic(Anthropic())
session = watch_mcp(mcp_session)
```

Wrap once; every call inside a `witness` block lands on the trajectory. No
handle to thread through your call sites.

`capture_content=False` records shape and metadata only — model, latency,
tokens, finish reason, tool names — and no prompt or completion text. Evidence a
customer refuses to enable is worth nothing.

### MCP tool-poisoning detection

A compromised MCP server can rewrite a tool's *description* mid-session to
inject instructions. The tool list looks identical; the instructions attached to
it changed. `watch_mcp` hashes each tool's name, description **and** input
schema, and a mid-session change:

1. raises an `mcp_manifest_changed` finding naming the changed tools, and
2. **taints the trajectory** — so a grant requiring a clean context refuses the
   next privileged action.

The injection is stopped, not merely noted afterwards.

## Taint is decided by the server

The SDK never sends a taint claim for a trajectory. The server reads what the
trajectory actually recorded. The agent this control defends against is exactly
the one that would report a clean context.
