"""Google Gemini middleware.

Wraps a `google-genai` client, and Vertex AI through the same shape. Duck-typed:
this module imports no provider library.

    from google import genai
    client = watch_gemini(genai.Client())

    with rs.witness("support-agent"):
        client.models.generate_content(model="gemini-2.0-flash", contents=[...])

subhadipmitra@: Gemini names almost everything differently from OpenAI, and the
mapping IS the work here. `usage_metadata` not `usage`, `candidates` not
`choices`, `finish_reason` as an enum rather than a string, and a function call
buried in `content.parts` rather than at the top level.

None of that is hard, but every one of them is a place where a naive port
records `None` and nobody notices — the call still succeeds, the trajectory
still appears, and the evidence is quietly empty. So each field is read
defensively and the shape is asserted by tests rather than assumed.
"""

import time
from typing import Any

from rotascale.client import current_trajectory
from rotascale.middleware._common import logger, report_served_model, truncate


def _enum_name(value: Any) -> Any:
    """Gemini returns enums where OpenAI returns strings.

    `FinishReason.STOP` serialises as something unhelpful in JSON, and an
    evidence store full of `"FinishReason.STOP"` is worse than one full of
    `"STOP"` — it leaks a Python repr into a compliance record.
    """
    if value is None:
        return None
    return getattr(value, "name", None) or str(value)


def _usage(response: Any) -> dict[str, Any] | None:
    # subhadipmitra@: On a streamed response only the FINAL chunk carries usage,
    # so a caller who aggregates themselves will see None here on every chunk
    # but the last. That is Gemini's shape, not a bug to paper over.
    meta = getattr(response, "usage_metadata", None)
    if meta is None:
        return None
    return {
        "prompt_tokens": getattr(meta, "prompt_token_count", None),
        "completion_tokens": getattr(meta, "candidates_token_count", None),
        # Gemini bills thinking tokens separately and they are invisible in the
        # other two. A cost question asked later cannot be answered without it.
        "thinking_tokens": getattr(meta, "thoughts_token_count", None),
        "total_tokens": getattr(meta, "total_token_count", None),
    }


def _function_calls(candidate: Any) -> list[str]:
    """Names of the functions the model asked to call.

    They live inside `content.parts`, one part per call, alongside text parts —
    so this walks the parts rather than reading a top-level field that does not
    exist.
    """
    content = getattr(candidate, "content", None)
    parts = getattr(content, "parts", None) or []
    names = []
    for part in parts:
        call = getattr(part, "function_call", None)
        if call is not None:
            name = getattr(call, "name", None)
            if name:
                names.append(name)
    return names


def _text(candidate: Any) -> str | None:
    content = getattr(candidate, "content", None)
    parts = getattr(content, "parts", None) or []
    chunks = [getattr(p, "text", None) for p in parts]
    joined = "".join(c for c in chunks if c)
    return joined or None


class _WatchedModels:
    def __init__(self, inner: Any, capture_content: bool, limit: int) -> None:
        self._inner = inner
        self._capture = capture_content
        self._limit = limit

    def generate_content(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            response = self._inner.generate_content(**kwargs)
        except Exception as exc:
            # A failed model call is evidence too: "the agent did nothing
            # because the provider was down" is a different story from "the
            # agent chose to do nothing".
            self._record_failure(kwargs, exc, started)
            raise
        self._record_success(kwargs, response, started)
        return response

    def _step(self, kwargs: dict) -> dict:
        config = kwargs.get("config")
        return {
            "provider": "google-gemini",
            "model_requested": kwargs.get("model"),
            "temperature": getattr(config, "temperature", None),
        }

    def _record_success(self, kwargs: dict, response: Any, started: float) -> None:
        trajectory = current_trajectory()
        if trajectory is None:
            return
        step = self._step(kwargs)
        step["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)

        # subhadipmitra@: `model_version` is the SERVED build, and Gemini
        # frequently answers a request for an alias with a dated one —
        # `gemini-2.0-flash` in, `gemini-2.0-flash-001` out. The served
        # identity is the evidence; the requested one is an intention.
        served = getattr(response, "model_version", None)
        step["model_served"] = served
        step["response_id"] = getattr(response, "response_id", None)
        report_served_model(served, "google-gemini")

        usage = _usage(response)
        if usage is not None:
            step["usage"] = usage

        candidates = getattr(response, "candidates", None) or []
        if candidates:
            first = candidates[0]
            step["finish_reason"] = _enum_name(getattr(first, "finish_reason", None))
            calls = _function_calls(first)
            if calls:
                # Names only. Arguments are the agent's business and land on
                # their own tool_call steps if the agent records them.
                step["tool_calls"] = calls
            if self._capture:
                step["response"] = truncate(_text(first), self._limit)

        # A response blocked by a safety filter has no candidates at all, and
        # the reason is somewhere else entirely. Without this the trajectory
        # would show an empty successful call, which reads as the model having
        # nothing to say.
        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None:
            blocked = _enum_name(getattr(feedback, "block_reason", None))
            if blocked:
                step["blocked_by_provider"] = blocked

        if self._capture:
            contents = kwargs.get("contents")
            if contents:
                last = contents[-1] if isinstance(contents, list) else contents
                step["last_message"] = truncate(
                    last if isinstance(last, str) else str(last), self._limit)
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


class _WatchedClient:
    def __init__(self, inner: Any, capture_content: bool, limit: int) -> None:
        self._inner = inner
        self.models = _WatchedModels(inner.models, capture_content, limit)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def watch_gemini(client: Any, *, capture_content: bool = True,
                 content_limit: int = 2000) -> Any:
    """Wrap a Gemini client so its calls land on the trajectory.

    Works for `google-genai` and for Vertex AI, which expose the same
    `client.models.generate_content` shape.

    `capture_content=False` records shape and metadata only — model, latency,
    tokens, finish reason, function names — and no prompt or response text.
    Evidence a customer refuses to enable is worth nothing.
    """
    return _WatchedClient(client, capture_content, content_limit)
