"""LangGraph middleware — records the PATH, not just the calls.

LangGraph runs on LangChain's callback machinery, so this extends that handler
rather than replacing it:

    from rotascale.middleware import watch_langgraph

    with rs.witness("contract-summariser", ref=doc_id) as t:
        graph.invoke(state, config={"callbacks": [watch_langgraph()]})

subhadipmitra@: A graph is not a chain, and the difference is the entire reason
this module exists.

A LangGraph run is a state machine: named nodes, conditional edges, loops.
Flattening it into a list of model calls throws away the thing actually worth
governing — **which path the agent took**. Two runs with identical LLM calls
and different traversals are different behaviours, and only one of them may be
the one that was approved.

So node executions are recorded with their visit index. A node entered forty
times reads as `analyse (visit 40)`, not as forty unexplained calls with no
indication they were the same step going round again. That is also what makes a
runaway loop legible in the console rather than merely large.

**On resume.** LangGraph checkpoints, so a run can continue in a different
process. Nothing special is needed here: `ref` is the SDK's idempotency key, so
a resumed run passing the same `ref` continues the SAME trajectory instead of
forking a second one. Pass the graph's thread id as `ref` and the record
survives the hop.
"""

from typing import Any

from rotascale.middleware._common import logger
from rotascale.middleware.langchain_api import RotascaleCallback

#: LangGraph's own bookkeeping nodes. Recording them buries the customer's
#: actual graph under machinery they did not write.
_INTERNAL_NODES = {"__start__", "__end__", "LangGraph", "RunnableSequence"}


class RotascaleGraphCallback(RotascaleCallback):
    """A LangChain callback that also reconstructs the graph traversal.

    Inherits the thread-binding fix from `RotascaleCallback` — LangGraph fires
    callbacks from the same thread pool, so the same trap applies and the same
    solution holds.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._visits: dict[str, int] = {}
        self._path: list[str] = []
        self._nodes: dict[Any, str] = {}

    # --- graph traversal ---------------------------------------------------

    def on_chain_start(self, serialized: dict, inputs: Any, *, run_id: Any = None,
                       metadata: dict | None = None, **kwargs: Any) -> None:
        node = (metadata or {}).get("langgraph_node")
        if node and node not in _INTERNAL_NODES:
            self._nodes[run_id] = node

    def on_chain_end(self, outputs: Any, *, run_id: Any = None,
                     **kwargs: Any) -> None:
        node = self._nodes.pop(run_id, None)
        if node is None or self._trajectory is None:
            return

        self._visits[node] = self._visits.get(node, 0) + 1
        visit = self._visits[node]
        self._path.append(node)

        try:
            self._trajectory.plan(
                framework="langgraph",
                node=node,
                # The number that turns forty calls into "a loop that ran forty
                # times". Without it a repeated node is indistinguishable from
                # forty different ones.
                visit=visit,
                position=len(self._path),
                # subhadipmitra@: Carried on every node so a trajectory read
                # from any single step still shows where in the traversal it
                # happened, without needing the whole sequence.
                path_so_far=self._path[-8:],
            )
        except Exception:
            logger.warning("rotascale: failed to record a graph node", exc_info=True)

    # --- what the run actually did ----------------------------------------

    @property
    def path(self) -> list[str]:
        """The node sequence, in order. Useful in assertions and in tests."""
        return list(self._path)

    @property
    def loops(self) -> dict[str, int]:
        """Nodes entered more than once, and how many times.

        subhadipmitra@: The question somebody asks after a surprising bill is
        "did it go round?", and this answers it without reading the trajectory.
        """
        return {node: n for node, n in self._visits.items() if n > 1}

    def summarise(self) -> None:
        """Record the traversal as one step. Call it after the graph returns.

        Optional: every node is already on the record. This adds the shape of
        the whole run in one place, which is what a reviewer reads first.
        """
        if self._trajectory is None:
            return
        try:
            self._trajectory.plan(
                framework="langgraph",
                traversal_complete=True,
                nodes_visited=len(self._path),
                distinct_nodes=len(self._visits),
                path=self._path,
                loops=self.loops,
            )
        except Exception:
            logger.warning("rotascale: failed to record the traversal",
                           exc_info=True)


def watch_langgraph(*, capture_content: bool = True,
                    content_limit: int = 2000) -> RotascaleGraphCallback:
    """Build a callback handler for LangGraph. Call it inside a witness block.

    Records every model call and tool call as the LangChain handler does, plus
    the node traversal — so the trajectory shows which path the graph took, not
    only what it called along the way.
    """
    return RotascaleGraphCallback(capture_content=capture_content,
                                  content_limit=content_limit)
