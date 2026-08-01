"""OpenAI-compatible middleware.

Wraps anything exposing `chat.completions.create` — the OpenAI SDK, Azure
OpenAI, Together, Groq, vLLM, Ollama, and every other service that copied the
shape. Duck-typed: this module imports no provider library.

    client = watch_openai(OpenAI())

    with rs.witness("support-agent"):
        client.chat.completions.create(model="gpt-4o", messages=[...])
        # the call is on the trajectory; no other change to the agent
"""

import time
from typing import Any

from rotascale.client import current_trajectory
from rotascale.middleware._common import logger, truncate


class _WatchedCompletions:
    def __init__(self, inner: Any, capture_content: bool, limit: int) -> None:
        self._inner = inner
        self._capture = capture_content
        self._limit = limit

    def create(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            response = self._inner.create(**kwargs)
        except Exception as exc:
            # subhadipmitra@: A failed model call is evidence too. "The agent did
            # nothing because the provider was down" is a materially different
            # story from "the agent chose to do nothing", and only one of them
            # is in the record if failures are dropped.
            self._record_failure(kwargs, exc, started)
            raise
        self._record_success(kwargs, response, started)
        return response

    def _step(self, kwargs: dict) -> dict:
        return {
            "provider": "openai-compatible",
            "model_requested": kwargs.get("model"),
            "temperature": kwargs.get("temperature"),
            "tool_choice": kwargs.get("tool_choice"),
        }

    def _record_success(self, kwargs: dict, response: Any, started: float) -> None:
        trajectory = current_trajectory()
        if trajectory is None:
            return
        step = self._step(kwargs)
        step["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        step["model_served"] = getattr(response, "model", None)
        step["response_id"] = getattr(response, "id", None)

        usage = getattr(response, "usage", None)
        if usage is not None:
            step["usage"] = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
            }

        choices = getattr(response, "choices", None) or []
        if choices:
            first = choices[0]
            step["finish_reason"] = getattr(first, "finish_reason", None)
            message = getattr(first, "message", None)
            calls = getattr(message, "tool_calls", None) if message else None
            if calls:
                # Tool NAMES only. The arguments are the agent's business and
                # land on their own tool_call steps if the agent records them.
                step["tool_calls"] = [
                    getattr(getattr(c, "function", None), "name", None) for c in calls
                ]
            if self._capture and message is not None:
                step["response"] = truncate(getattr(message, "content", None), self._limit)

        if self._capture:
            messages = kwargs.get("messages") or []
            if messages:
                last = messages[-1]
                step["last_message"] = truncate(
                    last.get("content") if isinstance(last, dict) else last, self._limit
                )
        try:
            trajectory.llm_call(**step)
        except Exception:
            logger.warning("rotascale: failed to record llm_call", exc_info=True)

    def _record_failure(self, kwargs: dict, exc: Exception, started: float) -> None:
        trajectory = current_trajectory()
        if trajectory is None:
            return
        step = self._step(kwargs)
        step["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        step["error"] = type(exc).__name__
        step["error_message"] = str(exc)[:500]
        try:
            trajectory.llm_call(**step)
        except Exception:
            logger.warning("rotascale: failed to record a failed llm_call", exc_info=True)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _WatchedChat:
    def __init__(self, inner: Any, capture_content: bool, limit: int) -> None:
        self._inner = inner
        self.completions = _WatchedCompletions(inner.completions, capture_content, limit)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _WatchedClient:
    def __init__(self, inner: Any, capture_content: bool, limit: int) -> None:
        self._inner = inner
        self.chat = _WatchedChat(inner.chat, capture_content, limit)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def watch_openai(client: Any, *, capture_content: bool = True, content_limit: int = 2000) -> Any:
    """Wrap an OpenAI-compatible client so its calls land on the trajectory.

    `capture_content=False` records shape and metadata only — model, latency,
    tokens, finish reason, tool names — and no prompt or completion text. Some
    deployments cannot put user content in a second store at all, and evidence
    that a customer refuses to enable is worth nothing.
    """
    return _WatchedClient(client, capture_content, content_limit)
