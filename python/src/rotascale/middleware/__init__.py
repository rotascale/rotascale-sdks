"""Framework middlewares.

Every one is duck-typed: none imports the library it wraps, so `from
rotascale.middleware import *` works with nothing else installed and the SDK
never drags a provider dependency into a customer's lockfile.

They attach to whatever trajectory is currently in scope, so instrumenting an
existing agent means wrapping the client once — not threading a handle through
every call site.
"""

from rotascale.middleware.adk_api import watch_adk
from rotascale.middleware.anthropic_api import watch_anthropic
from rotascale.middleware.autogen_api import watch_autogen
from rotascale.middleware.bedrock_api import watch_bedrock
from rotascale.middleware.crewai_api import record_delegation, watch_crew
from rotascale.middleware.gemini_api import watch_gemini
from rotascale.middleware.langchain_api import (
    RotascaleCallback,
    watch_langchain,
)
from rotascale.middleware.langgraph_api import (
    RotascaleGraphCallback,
    watch_langgraph,
)
from rotascale.middleware.mcp_api import (
    manifest_digest,
    split_digests,
    watch_mcp,
)
from rotascale.middleware.openai_compat import watch_openai
from rotascale.middleware.strands_api import watch_strands

__all__ = [
    "RotascaleCallback",
    "RotascaleGraphCallback",
    "manifest_digest",
    "record_delegation",
    "split_digests",
    "watch_adk",
    "watch_anthropic",
    "watch_autogen",
    "watch_bedrock",
    "watch_crew",
    "watch_gemini",
    "watch_langchain",
    "watch_langgraph",
    "watch_mcp",
    "watch_openai",
    "watch_strands",
]
