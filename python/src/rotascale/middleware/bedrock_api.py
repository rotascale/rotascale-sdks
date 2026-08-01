"""AWS Bedrock middleware.

Wraps a `bedrock-runtime` botocore client. Duck-typed: this module imports no
provider library, not even boto3.

    import boto3
    client = watch_bedrock(boto3.client("bedrock-runtime"))

    with rs.witness("underwriting-agent"):
        client.converse(modelId="us.anthropic.claude-sonnet-4-20250514-v1:0", ...)

subhadipmitra@: Bedrock is the awkward one, for three reasons worth naming.

**`converse` is normalised; `invoke_model` is not.** `converse` returns a shape
Bedrock defines. `invoke_model` returns whatever the underlying provider emits —
Anthropic-shaped JSON for Claude, Titan-shaped for Titan, Llama-shaped for
Llama — so reading usage at all means dispatching on the body. We read what we
recognise and record the rest as unknown rather than guessing.

**The model id carries the region and the inference profile.** `us.anthropic.…`
is not decoration: for a customer under a residency regime, which region served
the call is exactly the question an auditor asks. It is recorded verbatim and
the region prefix is pulled out, because nobody should have to parse it later.

**A streamed response is an EventStream, consumed once.** Wrapping it must not
swallow chunks from the caller, so streaming is recorded at call time and the
stream is handed back untouched.
"""

import json
import time
from typing import Any

from rotascale.client import current_trajectory
from rotascale.middleware._common import logger, report_served_model, truncate

#: Inference-profile prefixes Bedrock uses for cross-region routing.
_REGION_PREFIXES = ("us", "eu", "apac", "us-gov")


def _region_of(model_id: str | None) -> str | None:
    """The inference-profile region, if the id carries one.

    subhadipmitra@: `us.anthropic.claude-…` means the call may be served from
    any US region in the profile. A customer whose market profile forbids
    processing outside the EU needs that visible in the record, not buried in
    a string somebody has to know how to read.
    """
    if not model_id:
        return None
    head = model_id.split(".", 1)[0]
    return head if head in _REGION_PREFIXES else None


def _usage_from_converse(response: dict) -> dict[str, Any] | None:
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    return {
        "prompt_tokens": usage.get("inputTokens"),
        "completion_tokens": usage.get("outputTokens"),
        "total_tokens": usage.get("totalTokens"),
    }


def _usage_from_body(body: dict) -> dict[str, Any] | None:
    """Usage out of an `invoke_model` body, whose shape is the PROVIDER's.

    Each family names these differently and none of them is Bedrock's problem.
    Anything unrecognised returns None rather than a wrong number — a plausible
    but incorrect token count in a cost report is worse than a missing one.
    """
    # Anthropic on Bedrock
    usage = body.get("usage")
    if isinstance(usage, dict) and "input_tokens" in usage:
        return {"prompt_tokens": usage.get("input_tokens"),
                "completion_tokens": usage.get("output_tokens")}
    # Amazon Titan
    if "inputTextTokenCount" in body:
        results = body.get("results") or [{}]
        return {"prompt_tokens": body.get("inputTextTokenCount"),
                "completion_tokens": results[0].get("tokenCount")}
    # Meta Llama
    if "prompt_token_count" in body:
        return {"prompt_tokens": body.get("prompt_token_count"),
                "completion_tokens": body.get("generation_token_count")}
    return None


def _text_from_converse(response: dict) -> str | None:
    content = ((response.get("output") or {}).get("message") or {}).get("content") or []
    chunks = [block.get("text") for block in content if isinstance(block, dict)]
    joined = "".join(c for c in chunks if c)
    return joined or None


def _tools_from_converse(response: dict) -> list[str]:
    content = ((response.get("output") or {}).get("message") or {}).get("content") or []
    names = []
    for block in content:
        use = block.get("toolUse") if isinstance(block, dict) else None
        if isinstance(use, dict) and use.get("name"):
            names.append(use["name"])
    return names


class _WatchedBedrock:
    def __init__(self, inner: Any, capture_content: bool, limit: int) -> None:
        self._inner = inner
        self._capture = capture_content
        self._limit = limit

    # --- converse: the normalised path ------------------------------------

    def converse(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            response = self._inner.converse(**kwargs)
        except Exception as exc:
            self._failure("converse", kwargs, exc, started)
            raise

        step = self._base("converse", kwargs, started)
        step["stop_reason"] = response.get("stopReason")
        usage = _usage_from_converse(response)
        if usage:
            step["usage"] = usage
        tools = _tools_from_converse(response)
        if tools:
            step["tool_calls"] = tools
        if self._capture:
            step["response"] = truncate(_text_from_converse(response), self._limit)
        self._record(step)
        return response

    def converse_stream(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            response = self._inner.converse_stream(**kwargs)
        except Exception as exc:
            self._failure("converse_stream", kwargs, exc, started)
            raise
        # subhadipmitra@: The EventStream is consumed ONCE, by the caller.
        # Reading it here to enrich the record would silently empty their
        # response — the agent would receive nothing and the bug would look
        # like a provider fault. Recorded at call time, handed back untouched.
        step = self._base("converse_stream", kwargs, started)
        step["streamed"] = True
        self._record(step)
        return response

    # --- invoke_model: the provider-shaped path ---------------------------

    def invoke_model(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            response = self._inner.invoke_model(**kwargs)
        except Exception as exc:
            self._failure("invoke_model", kwargs, exc, started)
            raise

        step = self._base("invoke_model", kwargs, started)
        # The body is a streaming blob that the CALLER must be able to read.
        # botocore returns a StreamingBody; reading it here would leave them an
        # empty one, so this only looks when it can do so without consuming.
        body = response.get("body")
        payload = None
        if body is not None and hasattr(body, "read"):
            try:
                raw = body.read()
                payload = json.loads(raw)
                # Put it back. Without this the caller's `body.read()` returns
                # b"" and their agent sees an empty completion.
                response["body"] = _Replayed(raw)
            except Exception:
                logger.warning("rotascale: could not read a Bedrock body",
                               exc_info=True)

        if isinstance(payload, dict):
            usage = _usage_from_body(payload)
            if usage:
                step["usage"] = usage
            else:
                # Named, not silently absent. An unrecognised family is a gap
                # in this middleware, and it should be visible as one.
                step["usage_unavailable"] = "unrecognised invoke_model body shape"
            stop = payload.get("stop_reason") or payload.get("completionReason")
            if stop:
                step["stop_reason"] = stop
        self._record(step)
        return response

    def invoke_model_with_response_stream(self, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            response = self._inner.invoke_model_with_response_stream(**kwargs)
        except Exception as exc:
            self._failure("invoke_model_with_response_stream", kwargs, exc, started)
            raise
        step = self._base("invoke_model_with_response_stream", kwargs, started)
        step["streamed"] = True
        self._record(step)
        return response

    # --- shared ------------------------------------------------------------

    def _base(self, operation: str, kwargs: dict, started: float) -> dict:
        model_id = kwargs.get("modelId")
        step = {
            "provider": "aws-bedrock",
            "operation": operation,
            # Verbatim. It carries the family, the version AND the inference
            # profile, and re-deriving any of those later is guesswork.
            "model_requested": model_id,
            "model_served": model_id,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        region = _region_of(model_id)
        if region:
            step["inference_region"] = region
        report_served_model(model_id, "aws-bedrock")
        return step

    def _record(self, step: dict) -> None:
        trajectory = current_trajectory()
        if trajectory is None:
            return
        try:
            trajectory.llm_call(**step)
        except Exception:
            logger.warning("rotascale: failed to record llm_call", exc_info=True)

    def _failure(self, operation: str, kwargs: dict, exc: Exception,
                 started: float) -> None:
        step = self._base(operation, kwargs, started)
        step["error"] = type(exc).__name__
        step["error_message"] = str(exc)[:500]
        self._record(step)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _Replayed:
    """A botocore StreamingBody the caller can still read after we did."""

    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self, *args: Any, **kwargs: Any) -> bytes:
        return self._raw


def watch_bedrock(client: Any, *, capture_content: bool = True,
                  content_limit: int = 2000) -> Any:
    """Wrap a `bedrock-runtime` client so its calls land on the trajectory.

    Covers `converse`, `converse_stream`, `invoke_model` and
    `invoke_model_with_response_stream`.

    `capture_content=False` records shape and metadata only. Streaming calls
    record the request but not the completion — the stream belongs to the
    caller and reading it here would empty it.
    """
    return _WatchedBedrock(client, capture_content, content_limit)
