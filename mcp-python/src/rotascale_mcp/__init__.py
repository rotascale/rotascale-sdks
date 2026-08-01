"""Rotascale governance over the Model Context Protocol.

Two surfaces, and the difference between them matters:

  rotascale-mcp        governance as MCP tools. OPT-IN: the agent chooses to
                       call them, so this is evidence plus an advisory gate.
  rotascale-mcp-proxy  sits between the agent and its real MCP server. Every
                       tool call passes through. The agent CANNOT opt out.

Only the second is a control. Do not describe the first as one.
"""

from importlib.metadata import PackageNotFoundError, version

from rotascale_mcp.server import build, run

try:
    __version__ = version("rotascale-mcp")
except PackageNotFoundError:
    __version__ = "0.0.0.dev0"

__all__ = ["__version__", "build", "run"]
