"""Combined ASGI entry point: MCP server + agent backend in one process.

Route map:

- ``/mcp`` and ``/mcp/*`` -> the MCP server app (``mcp.server.MCPServer().create_app()``)
- everything else -> the agent backend (``agent.main:app``), when present

Until the agent backend lands (``agent/main.py``), non-``/mcp`` routes fall
back to a small placeholder so the same image and command work from day one.
The MCP surface is fully live either way.

Run::

    python -m deploy.serve                                  # 127.0.0.1:8080
    VAP_SERVE_HOST=0.0.0.0 VAP_SERVE_PORT=8080 python -m deploy.serve

Environment:

- ``VAP_SERVE_HOST`` -- bind address (default ``127.0.0.1``)
- ``VAP_SERVE_PORT`` -- bind port (default ``8080``)

LLM configuration for the agent backend is read from ``VAP_LLM_BASE_URL`` /
``VAP_LLM_MODEL`` / ``VAP_LLM_API_KEY`` (never logged, never echoed).
"""

from __future__ import annotations

import json
import os

from starlette.types import Receive, Scope, Send


def _load_agent_app():
    """Return the FastAPI agent app if ``agent/main.py`` exists (T4), else None.

    Only ImportError is swallowed ("not built yet"); any other failure to
    import a present agent is a real error and must surface loudly.
    """
    try:
        from agent.main import app  # type: ignore
    except ImportError:
        return None
    return app


class CombinedApp:
    """Pure ASGI dispatcher joining the MCP server and the agent backend."""

    def __init__(self) -> None:
        from mcp.server import MCPServer

        self.mcp_app = MCPServer().create_app()
        self.agent_app = _load_agent_app()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            # Inner ASGI apps are stateless here; forward lifespan to whichever
            # app may own startup/shutdown logic (agent first, MCP fallback).
            target = self.agent_app or self.mcp_app
            await target(scope, receive, send)
            return

        path = scope.get("path", "/")
        if path == "/mcp" or path.startswith("/mcp/"):
            await self.mcp_app(scope, receive, send)
            return

        if self.agent_app is not None:
            await self.agent_app(scope, receive, send)
            return

        await self._placeholder(scope, receive, send)

    async def _placeholder(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Minimal JSON app used until the agent backend is installed."""
        if scope["type"] != "http":
            return

        path = scope.get("path", "/")
        method = scope.get("method", "GET")

        if method == "GET" and path == "/healthz":
            status = 200
            payload = {
                "status": "ok",
                "mcp": "ok",
                "agent_installed": False,
                "version": "0.1.0",
            }
        elif method == "GET" and path == "/":
            status = 200
            payload = {
                "service": "virtuozzo-am-assistant",
                "mcp_endpoint": "/mcp",
                "health": "/healthz",
                "agent_installed": False,
                "note": "agent backend (agent/main.py) is not installed in this "
                        "build; the MCP tool surface is fully available",
            }
        else:
            status = 404
            payload = {
                "error": "not_found",
                "note": "agent backend not installed in this build; "
                        "MCP lives at POST /mcp",
            }

        body = json.dumps(payload).encode()
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def create_app() -> CombinedApp:
    return CombinedApp()


def main() -> None:
    import uvicorn

    host = os.environ.get("VAP_SERVE_HOST", "127.0.0.1")
    port = int(os.environ.get("VAP_SERVE_PORT", "8080"))
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
