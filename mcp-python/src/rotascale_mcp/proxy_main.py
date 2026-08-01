"""Console entry point: `rotascale-mcp-proxy`."""

import argparse
import asyncio
import logging
import os
import sys

from rotascale_mcp.proxy import run

USAGE = """\
  rotascale-mcp-proxy --agent refund-assistant -- npx -y @acme/mail-mcp

Put it in an MCP host config where the real server used to be:

  {
    "mcpServers": {
      "mailer": {
        "command": "rotascale-mcp-proxy",
        "args": ["--agent", "refund-assistant", "--", "npx", "-y", "@acme/mail-mcp"],
        "env": {"ROTASCALE_URL": "...", "ROTASCALE_API_KEY": "rota_live_..."}
      }
    }
  }
"""


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="rotascale-mcp-proxy",
        description="Govern every tool call an MCP agent makes. The agent "
                    "cannot opt out.",
        epilog=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--agent", required=True,
        help="stable slug for this agent, e.g. refund-assistant")
    parser.add_argument(
        "--ref", default=None,
        help="your identifier for this session, recorded on the trajectory")
    parser.add_argument(
        "command", nargs=argparse.REMAINDER,
        help="-- followed by the MCP server command to wrap")
    args = parser.parse_args()

    command = [c for c in args.command if c != "--"]
    if not command:
        parser.error("no server command given. Put it after --, e.g.\n" + USAGE)

    # stdout IS the protocol channel. Anything printed there corrupts the
    # stream in a way that surfaces as an unexplained client-side parse error,
    # so logging goes to stderr and nothing else may print.
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("ROTASCALE_MCP_LOG", "WARNING"),
        format="%(levelname)s %(name)s: %(message)s",
    )

    raise SystemExit(asyncio.run(run(args.agent, command, ref=args.ref)))


if __name__ == "__main__":
    main()
