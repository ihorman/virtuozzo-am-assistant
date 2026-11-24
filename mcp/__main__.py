"""CLI entry point: ``python -m mcp`` serves the Application Management Assistant MCP server.

Modes:
  --stdio   newline-delimited JSON-RPC on stdin/stdout (default). Auth via
            JELASTIC_HOST / JELASTIC_SESSION environment variables.
  --http    Streamable HTTP at http://<host>:<port>/mcp. Auth per request via
            X-Jelastic-Host / X-Jelastic-Session headers.
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mcp",
        description="Application Management Assistant MCP server — Virtuozzo Application Manager "
                    "(Jelastic/VAP) tools with session passthrough and "
                    "confirm-gated mutations.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--stdio", action="store_true",
                      help="serve on stdin/stdout (default)")
    mode.add_argument("--http", action="store_true",
                      help="serve Streamable HTTP at /mcp")
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8091,
                        help="HTTP port (default 8091)")
    args = parser.parse_args(argv)

    from .server import MCPServer

    server = MCPServer()
    if args.http:
        import uvicorn
        uvicorn.run(server.create_app(), host=args.host, port=args.port)
        return 0
    asyncio.run(server.run_stdio())
    return 0


if __name__ == "__main__":
    sys.exit(main())
