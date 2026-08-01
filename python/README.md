# rotascale — Python SDK

Govern what your agents are allowed to do, and prove what they did.

```bash
pip install rotascale
```

## Authentication

Issue a key in the console under **API keys**. It is shown once.

```bash
export ROTASCALE_API_KEY=rota_live_…      # or rota_test_… against a sandbox
export ROTASCALE_URL=https://rotascale.acme.internal
```

A key names a **workspace, not an agent** — one key serves a whole fleet, and
each agent identifies itself. So a new agent needs no new credential, and
rotating a key does not rewrite anyone's identity.

A key may record trajectories, report provenance and ask for authorisation. It
**cannot** issue or revoke authority, change an enforcement mode, or read the
audit trail. Those are governance acts and belong to a named person, so a
leaked key cannot widen its own permissions — the worst it can do is write
evidence.

`token=` is also accepted for a human's OIDC session, which is what you want in
a notebook, not in a deployed runtime.

## The whole happy path

```python
from rotascale import Rotascale, Gated

rs = Rotascale()                     # reads ROTASCALE_URL and ROTASCALE_API_KEY
agent = rs.agent("refund-agent")     # names itself; created on first sight

with rs.witness(agent, ref="TICKET-88123") as t:
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

## Agents name themselves

`rs.agent("refund-agent")` is safe on every process start. The slug is a name
**you** write and control — it survives redeployment and is legible in a diff,
which an opaque `agt_01KYY…` copied out of a console is not. Rotascale maps
`(workspace, slug)` to one agent and returns the same one thereafter.

There is no registration step. An agent appears in the inventory the moment it
speaks, because an inventory that depends on somebody remembering to register is
incomplete by default — and an agent nobody registered is not an unregistered
agent, it is an ungoverned one.

**What appears automatically holds nothing.** A newly discovered agent records
evidence but has no authority and cannot be granted any until a named human
claims it in the console. That is the half that keeps the inventory *governed*
rather than self-asserted: otherwise anyone holding a key could mint a governed
principal just by naming one.

```python
agent = rs.agent("refund-agent")
if not agent.governed:
    log.warning("%s is not claimed yet — nothing is being enforced", agent.slug)
```

The SDK logs that warning for you at startup, where somebody is still watching,
rather than leaving you to discover it at the first refusal.

Slugs are **validated, not cleaned**. `refund_assistant` and `refund-assistant`
are two different agents, and a slug that cannot work is refused with a
suggestion rather than quietly rewritten. Silently normalising would merge two
programs onto one record, and the evidence would then say one agent did what two
of them did. A typo making a second agent is visible and fixable; a merge is
neither.

A slug can never be reassigned — the database refuses it, not just the API.

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

One line per framework. Every one is duck-typed — none imports the library it
wraps — so `pip install rotascale` never drags a provider dependency into your
lockfile. A governance library that forces a version conflict is one that does
not get installed.

```python
from rotascale.middleware import (
    watch_openai, watch_anthropic, watch_gemini, watch_bedrock,
    watch_langchain, watch_langgraph, watch_adk, watch_crew,
    watch_strands, watch_autogen, watch_mcp,
)

client = watch_openai(OpenAI())        # or Azure, Together, Groq, vLLM, Ollama…
claude = watch_anthropic(Anthropic())
gemini = watch_gemini(genai.Client())  # or Vertex AI
bedrock = watch_bedrock(boto3.client("bedrock-runtime"))
```

LangChain and LangGraph are **callback handlers**, because that is the
extension point those frameworks provide. Build them **inside** the witness
block — LangChain fires callbacks from a thread pool, and a handler built
outside would silently record nothing:

```python
with rs.witness(agent, ref=ticket) as t:
    chain.invoke(x, config={"callbacks": [watch_langchain()]})
    graph.invoke(state, config={"callbacks": [watch_langgraph()]})
```

### What each one adds beyond the model call

| | |
|---|---|
| **Gemini** | thinking tokens, which are billed separately and invisible in the other counts; a safety block, which returns no candidates at all |
| **Bedrock** | the inference region out of `us.anthropic.…`, which is what an auditor asks about under a residency regime |
| **LangGraph** | the node traversal, with visit counts — a loop reads as a loop, not as forty unexplained calls |
| **ADK** | **enforcement.** See below |
| **CrewAI** | the hand-off between agents |
| **Strands** | the tool manifest, read off the registry rather than retyped |
| **AutoGen** | each turn, and a round cap hit without terminating — recorded as a finding, because it was stopped by a limit rather than by a decision |

### ADK can actually refuse

Every other middleware here **observes**. ADK's `before_tool_callback` can
return a value that short-circuits the call, so a refusal stops the tool in the
tool path:

```python
watch_adk(agent, grant=GRANT)     # tool calls are authorised before they run
```

Without `grant=` it observes like the others. With it, ADK joins
`rotascale-mcp-proxy` as one of two places a refusal is a control rather than a
record of one. Worth being precise about, because the difference is what a
customer is buying.

`capture_content=False` on any of them records shape and metadata only — model,
latency, tokens, finish reason, tool names — and no prompt or completion text.
Evidence a customer refuses to enable is worth nothing.

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
