#!/usr/bin/env python3
"""Do the real provider APIs still match what the middlewares assume?

    uv run --with openai --with anthropic --with google-genai \
        python scripts/validate_providers.py

subhadipmitra@: Every other test in this package uses a fake written from the
documentation. That proves a middleware handles the shape we BELIEVE a provider
returns — which is exactly the belief worth checking, because a wrong one
records `None` silently: the call succeeds, the trajectory appears, and the
evidence is quietly empty.

The first run of this found two bugs nothing else could have:

  - Anthropic recorded `input_tokens` where every other middleware records
    `prompt_tokens`. Right numbers, wrong names, so usage could not be summed
    across providers. The fake in the tests had the same mistake baked in, so
    the test agreed with the bug.
  - `watch_gemini(genai.Client()).models.…` let the provider's client be
    garbage-collected mid-use, because the nested wrapper held `.models` and
    not the root. The fakes have no `__del__`, so nothing could surface it.

It also found that `gemini-2.0-flash` had been retired and returns 404. That is
the other reason this runs on a schedule: providers change under us, and we
would rather learn it from a red build than from a customer.

Costs a fraction of a cent per run. Prints no key material. A provider whose key
is absent is SKIPPED and said so — never quietly passed.
"""

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

from rotascale.middleware import (  # noqa: E402
    watch_anthropic,
    watch_gemini,
    watch_openai,
)

PROMPT = "Reply with exactly one word: APPROVED"

#: Cheap, current models. Update when a provider retires one — which is a thing
#: this script exists to tell you about.
OPENAI_MODEL = os.environ.get("VALIDATE_OPENAI_MODEL", "gpt-4o-mini")
ANTHROPIC_MODEL = os.environ.get("VALIDATE_ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
GEMINI_MODEL = os.environ.get("VALIDATE_GEMINI_MODEL", "gemini-3.5-flash")

captured: list[dict] = []


def _recorder() -> None:
    """Put a trajectory in scope that captures steps instead of sending them."""
    # subhadipmitra@: A stub rather than a real client. The middlewares call
    # `report_provenance` on the first served model, and a real client would
    # try to POST it — producing a ConnectError traceback in the output of a
    # script whose whole job is to make a shape mismatch easy to see.
    class _Silent:
        def report_provenance(self, *args, **kwargs):
            return None

    class _Capture:
        id = "trj_validate"
        agent_id = "agt_validate"
        _closed = False
        _token = None
        _client = _Silent()

        def llm_call(self, **payload):
            captured.append(payload)

        def __getattr__(self, name):
            return lambda *a, **kw: None

    import rotascale.client as client_module
    client_module._current.set(_Capture())


def _check(label: str, expected: dict[str, str], actual: dict) -> bool:
    print(f"\n{label}")
    ok = True
    for field, why in expected.items():
        value = actual.get(field)
        present = value not in (None, {}, [])
        if not present:
            ok = False
        print(f"  [{'ok  ' if present else 'MISS'}] {field:22} "
              f"{value if present else '— expected ' + why}")
    return ok


def main() -> int:
    _recorder()
    results: dict[str, bool | None] = {}

    if os.environ.get("OPENAI_API_KEY"):
        try:
            from openai import OpenAI
            captured.clear()
            watch_openai(OpenAI()).chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": PROMPT}], max_tokens=5)
            results["openai"] = _check(f"OpenAI ({OPENAI_MODEL})", {
                "model_served": "the dated build, not the alias",
                "usage": "prompt_tokens / completion_tokens",
                "finish_reason": "why it stopped",
                "response": "the text",
            }, captured[-1])
        except Exception as exc:
            print(f"\nOpenAI: FAILED — {type(exc).__name__}: {exc}")
            results["openai"] = False
    else:
        results["openai"] = None

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from anthropic import Anthropic
            captured.clear()
            watch_anthropic(Anthropic()).messages.create(
                model=ANTHROPIC_MODEL, max_tokens=5,
                messages=[{"role": "user", "content": PROMPT}])
            results["anthropic"] = _check(f"Anthropic ({ANTHROPIC_MODEL})", {
                "model_served": "the dated build",
                # The specific regression: these must be the NORMALISED names.
                "usage": "prompt_tokens / completion_tokens, not input/output",
                "stop_reason": "why it stopped",
            }, captured[-1])
            usage = captured[-1].get("usage") or {}
            if "input_tokens" in usage:
                print("  [MISS] usage uses Anthropic's own field names; they must "
                      "be normalised or usage cannot be summed across providers")
                results["anthropic"] = False
        except Exception as exc:
            print(f"\nAnthropic: FAILED — {type(exc).__name__}: {exc}")
            results["anthropic"] = False
    else:
        results["anthropic"] = None

    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        try:
            from google import genai
            captured.clear()
            watch_gemini(genai.Client(api_key=key)).models.generate_content(
                model=GEMINI_MODEL, contents=PROMPT)
            results["gemini"] = _check(f"Gemini ({GEMINI_MODEL})", {
                "model_served": "model_version, NOT model",
                "usage": "usage_metadata, NOT usage",
                "finish_reason": "an enum unwrapped to its .name",
                "response": "text joined from content.parts",
            }, captured[-1])
            # Not a hard failure — a trivial prompt may not think — but worth
            # seeing, because thinking tokens are billed and are invisible in
            # the other two counts.
            thinking = (captured[-1].get("usage") or {}).get("thinking_tokens")
            if thinking:
                print(f"  [note] thinking_tokens {thinking} — billed, and absent "
                      f"from prompt/completion")
        except Exception as exc:
            print(f"\nGemini: FAILED — {type(exc).__name__}: {exc}")
            results["gemini"] = False
    else:
        results["gemini"] = None

    print("\n" + "-" * 60)
    skipped = [p for p, r in results.items() if r is None]
    failed = [p for p, r in results.items() if r is False]
    passed = [p for p, r in results.items() if r is True]

    if passed:
        print(f"validated: {', '.join(passed)}")
    if skipped:
        # Named, never silent. A skipped provider is an unvalidated one.
        print(f"SKIPPED (no key): {', '.join(skipped)}")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    if not passed:
        print("nothing was validated")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
