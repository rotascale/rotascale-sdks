"""LangChain middleware — a callback handler, not a client wrapper.

LangChain has a first-class extension point, so this plugs into it rather than
wrapping a client:

    from rotascale.middleware import RotascaleCallback

    with rs.witness("invoice-triage") as t:
        handler = RotascaleCallback()
        chain.invoke({"input": "..."}, config={"callbacks": [handler]})

Still duck-typed: we implement the method names LangChain calls on a plain
object and import nothing from `langchain`.

subhadipmitra@: There is one trap here and it is worth the whole module.

**LangChain fires callbacks from a thread pool for sync chains**, and from the
event loop for async ones. The active trajectory is a `contextvars.ContextVar`,
and a ContextVar set on the main thread is NOT visible inside a worker thread.

So a handler that called `current_trajectory()` inside `on_llm_end` would work
perfectly in an async chain, record nothing at all in a sync one, and raise no
error either way. Silent, execution-model-dependent evidence loss — in the
integration most LangChain users would reach for first.

The handler therefore binds the trajectory AT CONSTRUCTION, on the caller's
thread, where the ContextVar is definitely set. That is why `RotascaleCallback`
must be created inside the `witness` block rather than at module scope, and why
it says so loudly if it was not.
"""

import time
from typing import Any

from rotascale.client import current_trajectory
from rotascale.middleware._common import logger, report_served_model, truncate


class RotascaleCallback:
    """A LangChain callback handler that records onto the active trajectory.

    Construct it INSIDE a `witness` block. LangChain may invoke it from another
    thread, where the active trajectory would be invisible, so it is captured
    here and held.
    """

    # LangChain checks these attributes on a handler.
    ignore_llm = False
    ignore_chain = False
    ignore_agent = False
    ignore_retriever = False
    ignore_chat_model = False
    raise_error = False
    run_inline = False

    def __init__(self, *, capture_content: bool = True,
                 content_limit: int = 2000) -> None:
        # Bound now, on the caller's thread. See the module docstring.
        self._trajectory = current_trajectory()
        self._capture = capture_content
        self._limit = content_limit
        self._started: dict[Any, float] = {}

        if self._trajectory is None:
            # Loud, because the alternative is a handler that silently records
            # nothing and a customer who believes their chain is governed.
            logger.warning(
                "rotascale: RotascaleCallback was created outside a witness "
                "block, so it has no trajectory to record onto and will do "
                "nothing. Construct it inside `with rs.witness(agent):`."
            )

    # --- LLM ---------------------------------------------------------------

    def on_llm_start(self, serialized: dict, prompts: list, *,
                     run_id: Any = None, **kwargs: Any) -> None:
        self._started[run_id] = time.perf_counter()

    on_chat_model_start = on_llm_start

    def on_llm_end(self, response: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        if self._trajectory is None:
            return
        step: dict[str, Any] = {"provider": "langchain"}
        started = self._started.pop(run_id, None)
        if started is not None:
            step["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)

        output = getattr(response, "llm_output", None) or {}
        served = output.get("model_name") or output.get("model")
        if served:
            step["model_served"] = served
            report_served_model(served, "langchain")

        usage = output.get("token_usage") or output.get("usage") or {}
        if usage:
            step["usage"] = {
                "prompt_tokens": usage.get("prompt_tokens") or usage.get("input_tokens"),
                "completion_tokens": (usage.get("completion_tokens")
                                      or usage.get("output_tokens")),
            }

        if self._capture:
            generations = getattr(response, "generations", None) or []
            if generations and generations[0]:
                step["response"] = truncate(
                    getattr(generations[0][0], "text", None), self._limit)
        self._record("llm_call", step)

    def on_llm_error(self, error: BaseException, *, run_id: Any = None,
                     **kwargs: Any) -> None:
        self._started.pop(run_id, None)
        self._record("llm_call", {
            "provider": "langchain",
            "error": type(error).__name__,
            "error_message": str(error)[:500],
        })

    # --- tools -------------------------------------------------------------

    def on_tool_start(self, serialized: dict, input_str: str, *,
                      run_id: Any = None, **kwargs: Any) -> None:
        self._started[run_id] = time.perf_counter()
        name = (serialized or {}).get("name") or "unknown"
        if self._trajectory is None:
            return
        try:
            # subhadipmitra@: A tool result is UNTRUSTED unless the caller says
            # otherwise. A LangChain retriever pulling a document is exactly the
            # taint source the `gated` outcome exists for, and defaulting to
            # trusted would quietly disable that control for every LangChain
            # user.
            self._trajectory.tool_call(name, trusted=False,
                                       framework="langchain")
        except Exception:
            logger.warning("rotascale: failed to record a tool call", exc_info=True)

    def on_retriever_start(self, serialized: dict, query: str, *,
                           run_id: Any = None, **kwargs: Any) -> None:
        if self._trajectory is None:
            return
        name = (serialized or {}).get("name") or "retriever"
        try:
            # A retrieval, not a tool call: this is content entering the
            # context, which is what taint tracks.
            self._trajectory.retrieval(f"langchain:{name}", trusted=False,
                                       query=truncate(query, 200) if self._capture else None)
        except Exception:
            logger.warning("rotascale: failed to record a retrieval", exc_info=True)

    def on_tool_error(self, error: BaseException, *, run_id: Any = None,
                      **kwargs: Any) -> None:
        self._started.pop(run_id, None)
        self._record("step", {"kind": "tool_call", "error": type(error).__name__,
                              "error_message": str(error)[:500]})

    # --- chains ------------------------------------------------------------

    def on_chain_error(self, error: BaseException, *, run_id: Any = None,
                       **kwargs: Any) -> None:
        self._record("plan", {"error": type(error).__name__,
                              "error_message": str(error)[:500]})

    # --- everything else is a no-op ---------------------------------------

    def __getattr__(self, name: str) -> Any:
        """LangChain calls many hooks; the ones we do not implement must not
        raise. A callback that throws takes the customer's chain down with it."""
        if name.startswith("on_"):
            return lambda *a, **kw: None
        raise AttributeError(name)

    def _record(self, method: str, payload: dict) -> None:
        if self._trajectory is None:
            return
        try:
            if method == "step":
                kind = payload.pop("kind", "tool_call")
                self._trajectory.step(kind, **payload)
            else:
                getattr(self._trajectory, method)(**payload)
        except Exception:
            logger.warning("rotascale: failed to record %s", method, exc_info=True)


def watch_langchain(*, capture_content: bool = True,
                    content_limit: int = 2000) -> RotascaleCallback:
    """Build a callback handler for LangChain. Call it inside a witness block.

        with rs.witness(agent) as t:
            chain.invoke(x, config={"callbacks": [watch_langchain()]})
    """
    return RotascaleCallback(capture_content=capture_content,
                             content_limit=content_limit)
