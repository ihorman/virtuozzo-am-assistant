"""MCP server for VAP Copilot: Streamable HTTP (``/mcp``) + stdio.

Transport contract (docs/API-CONTRACT.md):

- **HTTP mode** — POST JSON-RPC to ``/mcp``; per-request auth via
  ``X-Jelastic-Host`` / ``X-Jelastic-Session`` headers.
- **stdio mode** — newline-delimited JSON-RPC; auth via ``JELASTIC_HOST`` /
  ``JELASTIC_SESSION`` environment variables.

The server is stateless (one tool call = one JelasticClient with the caller's
session, closed afterwards). ``Mcp-Session-Id`` is echoed for client
compatibility but no server-side session state exists. GET/DELETE on ``/mcp``
return 405 (no server-initiated SSE stream, no session termination) — both
are legal per the Streamable HTTP spec.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from typing import Any, Optional, TextIO

from . import protocol
from .protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    JsonRpcError,
    LATEST_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    is_notification,
    make_error,
    make_result,
    validate_message,
)
from .tools import AuthConfig, NoCredentialsError, TOOLS, execute_tool

SERVER_NAME = "virtuozzo-am-mcp"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = (
    "Virtuozzo Application Manager (Jelastic/VAP) tools, acting strictly with "
    "the caller's own platform session. Read tools are free; every mutating "
    "tool requires confirm=true and MUST only be called after the user "
    "explicitly confirmed the exact action; env_delete additionally requires "
    "the account password (forwarded once, never stored). Jelastic operations "
    "are fire-and-forget: use op_result to poll, and never report a timeout "
    "as a failure."
)


class MCPServer:
    """Protocol core + transports. Construct with ``transport=`` to point the
    platform client at an in-process ASGI app (tests) instead of the network."""

    def __init__(self, *, transport=None, timeout: float = 30.0,
                 poll_interval: float = 0.25):
        self.transport = transport
        self.timeout = timeout
        self.poll_interval = poll_interval

    # -- protocol core ------------------------------------------------------

    async def handle_message(self, message: Any,
                             *, auth: Optional[AuthConfig] = None) -> Optional[dict]:
        """Handle one decoded JSON-RPC message. Returns the response dict,
        or None for notifications / client responses."""
        try:
            message = validate_message(message)
        except JsonRpcError as exc:
            return exc.to_response(None)

        if is_notification(message):
            return None  # incl. notifications/initialized
        if "method" not in message:
            return None  # a client response; we never send requests
        if not isinstance(message.get("id"), (str, int)):
            return None

        request_id = message["id"]
        method = message["method"]
        params = message.get("params") or {}

        try:
            result = await self._dispatch(method, params, auth)
        except JsonRpcError as exc:
            return exc.to_response(request_id)
        except Exception as exc:  # noqa: BLE001 - internal error must not crash
            return make_error(request_id, INTERNAL_ERROR,
                              f"internal error: {exc}")
        return make_result(request_id, result)

    async def handle_payload(self, payload: Any,
                             *, auth: Optional[AuthConfig] = None) -> Optional[Any]:
        """Handle a decoded POST body: single message or JSON-RPC batch.
        Returns the response (dict / list of dicts), or None when there is
        nothing to answer (202 Accepted)."""
        if isinstance(payload, list):
            if not payload:
                return make_error(None, INVALID_REQUEST, "empty batch")
            responses = []
            for item in payload:
                response = await self.handle_message(item, auth=auth)
                if response is not None:
                    responses.append(response)
            return responses or None
        return await self.handle_message(payload, auth=auth)

    async def _dispatch(self, method: str, params: dict,
                        auth: Optional[AuthConfig]) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [spec.to_mcp() for spec in TOOLS.values()]}
        if method == "tools/call":
            return await self._tools_call(params, auth)
        raise JsonRpcError(METHOD_NOT_FOUND, f"method not found: {method}")

    @staticmethod
    def _initialize(params: dict) -> dict:
        requested = params.get("protocolVersion")
        if requested in SUPPORTED_PROTOCOL_VERSIONS:
            version = requested
        else:
            version = LATEST_PROTOCOL_VERSION
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": INSTRUCTIONS,
        }

    async def _tools_call(self, params: dict,
                          auth: Optional[AuthConfig]) -> dict:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise JsonRpcError(INVALID_PARAMS, "tools/call requires [name]")
        if name not in TOOLS:
            raise JsonRpcError(INVALID_PARAMS, f"unknown tool [{name}]")
        arguments = params.get("arguments") or {}
        payload, is_error = await execute_tool(
            name, arguments, auth,
            transport=self.transport, timeout=self.timeout,
            poll_interval=self.poll_interval)
        return {
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "isError": is_error,
        }

    # -- Streamable HTTP transport ------------------------------------------

    def create_app(self):
        """ASGI app serving MCP at POST /mcp (plus /healthz for liveness)."""
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route

        server = self

        async def mcp_endpoint(request: Request):
            if request.method in ("GET", "DELETE"):
                # Legal per Streamable HTTP: no server-initiated stream /
                # no session termination support.
                return Response(status_code=405, headers={"Allow": "POST"})
            auth = None
            try:
                auth = AuthConfig.from_headers(request.headers)
            except NoCredentialsError:
                auth = None  # surfaced in-band as no_credentials per tool call
            try:
                payload = await request.json()
            except ValueError:
                return JSONResponse(
                    make_error(None, PARSE_ERROR, "body is not valid JSON"),
                    status_code=400)
            response = await server.handle_payload(payload, auth=auth)
            if response is None:
                return Response(status_code=202)
            headers = {}
            if isinstance(payload, dict) and \
                    payload.get("method") == "initialize":
                session_id = request.headers.get("mcp-session-id") \
                    or uuid.uuid4().hex
                headers["Mcp-Session-Id"] = session_id
            return JSONResponse(response, headers=headers)

        async def healthz(request: Request):
            return JSONResponse({"status": "ok", "service": SERVER_NAME,
                                 "version": SERVER_VERSION})

        return Starlette(routes=[
            Route("/mcp", mcp_endpoint, methods=["POST", "GET", "DELETE"]),
            Route("/healthz", healthz, methods=["GET"]),
        ])

    # -- stdio transport ----------------------------------------------------

    async def run_stdio(self, stdin: Optional[TextIO] = None,
                        stdout: Optional[TextIO] = None,
                        env: Optional[dict] = None) -> None:
        """Newline-delimited JSON-RPC over stdin/stdout (MCP stdio mode).
        Auth comes from JELASTIC_HOST / JELASTIC_SESSION."""
        import asyncio

        in_stream: TextIO = sys.stdin if stdin is None else stdin
        out_stream: TextIO = sys.stdout if stdout is None else stdout
        try:
            auth: Optional[AuthConfig] = AuthConfig.from_env(
                os.environ if env is None else env)
        except NoCredentialsError:
            auth = None  # per-call no_credentials error results

        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, in_stream.readline)
            if line in ("", None):
                break
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError as exc:
                response = make_error(None, PARSE_ERROR, f"invalid JSON: {exc}")
            else:
                response = await self.handle_payload(payload, auth=auth)
            if response is not None:
                out_stream.write(json.dumps(response) + "\n")
                out_stream.flush()
