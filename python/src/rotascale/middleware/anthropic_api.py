"""Anthropic middleware.

Wraps any client exposing `messages.create`. Duck-typed: imports no anthropic
package.

    client = watch_anthropic(Anthropic())
"""

import time
from typing import Any

from rotascale.client import current_trajectory
from rotascale.middleware._common import logger, report_served_model, truncate


class _WatchedMessages:
    def __init__(self, inner: Any, capture_content: bool, limit: int) -> None:
        self._inner = inner
        self._capture = capture_content
        self._limit = limit

    def create(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            response = self._inner.create(**kwargs)
        except Exception as exc:
            self._record({"error": type(exc).__name__, "error_message": str(exc)[:500]},
                         kwargs, started)
            raise
        step: dict[str, Any] = {
            "model_served": getattr(response, "model", None),
            "response_id": getattr(response, "id", None),
            "stop_reason": getattr(response, "stop_reason", None),
        }
        # The inventory learns what actually ran, from the runtime that ran it.
        # Once per (agent, model), not once per call.
        report_served_model(step["model_served"], "anthropic")
        usage = getattr(response, "usage", None)
        if usage is not None:
            step["usage"] = {
                # subhadipmitra@: NORMALISED, not Anthropic's own names.
                #
                # Every other middleware records prompt_tokens/completion_tokens.
                # This one recorded Anthropic's input_tokens/output_tokens, so a
                # query summing usage across a workspace silently missed every
                # Anthropic call — the numbers were right and the field names
                # were wrong, which is the hardest kind of wrong to notice.
                #
                # Found by calling the real API. The fake in the tests had the
                # same mistake baked in, so the test agreed with the bug.
                "prompt_tokens": getattr(usage, "input_tokens", None),
                "completion_tokens": getattr(usage, "output_tokens", None),
                # Anthropic-specific and worth keeping: cache reads are billed
                # differently and are invisible in the other two.
                "cache_read_tokens": getattr(usage, "cache_read_input_tokens", None),
            }
        content = getattr(response, "content", None) or []
        # A tool_use block means the model asked to act — the interesting part.
        step["tool_uses"] = [
            getattr(b, "name", None) for b in content if getattr(b, "type", None) == "tool_use"
        ] or None
        if self._capture and content:
            step["response"] = truncate(getattr(content[0], "text", None), self._limit)
        self._record(step, kwargs, started)
        return response

    def _record(self, step: dict, kwargs: dict, started: float) -> None:
        trajectory = current_trajectory()
        if trajectory is None:
            return
        step.update(
            provider="anthropic",
            model_requested=kwargs.get("model"),
            max_tokens=kwargs.get("max_tokens"),
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        if self._capture:
            messages = kwargs.get("messages") or []
            if messages:
                last = messages[-1]
                step["last_message"] = truncate(
                    last.get("content") if isinstance(last, dict) else last, self._limit
                )
        try:
            trajectory.llm_call(**{k: v for k, v in step.items() if v is not None})
        except Exception:
            logger.warning("rotascale: failed to record llm_call", exc_info=True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _WatchedAnthropic:
    def __init__(self, inner: Any, capture_content: bool, limit: int) -> None:
        self._inner = inner
        self.messages = _WatchedMessages(inner.messages, capture_content, limit)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def watch_anthropic(
    client: Any, *, capture_content: bool = True, content_limit: int = 2000
) -> Any:
    return _WatchedAnthropic(client, capture_content, content_limit)
