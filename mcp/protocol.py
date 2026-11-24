"""JSON-RPC 2.0 + MCP wire protocol helpers for the Virtuozzo Application Management Assistant MCP server.

Why not the PyPI ``mcp`` SDK: this repo's top-level package is itself named
``mcp`` (docs/PLAN.md section 8), which shadows the ``mcp`` SDK on
``sys.path`` — ``import mcp.server`` resolves to *this* package and fails.
The required surface is small and stable (``initialize`` handshake,
``tools/list``, ``tools/call``, ``ping`` over Streamable HTTP + stdio), so the
server implements it directly against the MCP specification:

- JSON-RPC 2.0 messages (single or batch).
- Protocol versions: 2024-11-05, 2025-03-26, 2025-06-18.
- Streamable HTTP transport: POST /mcp with ``application/json`` responses
  (spec allows a plain JSON response when no streaming is needed), optional
  ``Mcp-Session-Id`` echo, GET/DELETE answered 405 (no server-initiated
  stream, no session termination).
- stdio transport: newline-delimited JSON-RPC.

Nothing here talks about tools or Jelastic — see :mod:`mcp.tools` and
:mod:`mcp.server`.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Union

JSONRPC_VERSION = "2.0"

#: newest MCP specification revision this server speaks
LATEST_PROTOCOL_VERSION = "2025-06-18"
#: every revision whose message shapes we accept (negotiation echoes these)
SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")

# JSON-RPC 2.0 standard error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class JsonRpcError(Exception):
    """A JSON-RPC level error (as opposed to a tool execution error)."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_response(self, request_id: Any = None) -> dict:
        return make_error(request_id, self.code, self.message, self.data)


def make_result(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def make_error(request_id: Any, code: int, message: str,
               data: Any = None) -> dict:
    error: dict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def make_notification(method: str, params: Optional[dict] = None) -> dict:
    message: dict = {"jsonrpc": JSONRPC_VERSION, "method": method}
    if params is not None:
        message["params"] = params
    return message


def is_request(message: dict) -> bool:
    """A message that expects a response: has a method and an id."""
    return isinstance(message, dict) and "method" in message and "id" in message


def is_notification(message: dict) -> bool:
    """A message that must not be answered: has a method, no id."""
    return isinstance(message, dict) and "method" in message and "id" not in message


def is_response(message: dict) -> bool:
    """A client->server response (to a server-initiated request; we send
    none, so these are acknowledged and ignored)."""
    return (isinstance(message, dict) and "method" not in message
            and ("result" in message or "error" in message))


def validate_message(message: Any) -> dict:
    """Validate a decoded JSON-RPC message; raise :class:`JsonRpcError`."""
    if not isinstance(message, dict):
        raise JsonRpcError(INVALID_REQUEST, "message must be a JSON object")
    if message.get("jsonrpc") != JSONRPC_VERSION:
        raise JsonRpcError(INVALID_REQUEST,
                           "jsonrpc must be \"2.0\"", {"got": message.get("jsonrpc")})
    if "method" in message:
        if not isinstance(message["method"], str) or not message["method"]:
            raise JsonRpcError(INVALID_REQUEST, "method must be a non-empty string")
        if "id" in message and not isinstance(message["id"], (str, int)):
            raise JsonRpcError(INVALID_REQUEST, "id must be a string or integer")
        if "params" in message and not isinstance(message["params"], dict):
            raise JsonRpcError(INVALID_PARAMS, "params must be an object")
        return message
    if "result" in message or "error" in message:
        return message
    raise JsonRpcError(INVALID_REQUEST,
                       "message is neither a request, notification nor response")


def parse_message(raw: Union[str, bytes]) -> Any:
    """Decode one JSON-RPC message (or batch). Raises :class:`JsonRpcError`
    on invalid JSON (parse error) — per JSON-RPC 2.0 the parse-error id is
    null."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise JsonRpcError(PARSE_ERROR, f"invalid JSON: {exc}") from exc
    return payload
