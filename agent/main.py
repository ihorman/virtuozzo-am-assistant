"""FastAPI app for the VAP Copilot agent backend (docs/API-CONTRACT.md).

Routes:
- ``POST /api/chat``   — SSE stream (events exactly per the frozen contract)
- ``POST /api/answer`` — resolves a paused ``ask_user`` / ``confirm_action``
  or a clicked card action (additive v1.2 answer shape, see agent/README.md)
- ``GET /api/context`` — echo of the last ``X-Vap-Context``
- ``GET /api/audit``   — mutation audit trail for the conversation
- ``GET /healthz``     — liveness + LLM configured (never echoes secrets)

The LLM endpoint is deployment-time configuration (``VAP_LLM_*`` env vars,
any OpenAI-compatible service) — nothing about a provider is hardcoded.

Wiring points for tests: ``create_app(llm_factory=..., transport=...,
verifier=...)`` injects a FakeLLM, the mock platform's ASGI transport and a
fake URL verifier, so the whole stack runs network-free.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from mcp.tools import AuthConfig, NoCredentialsError

from .config import Settings
from .conversation import AnswerMismatch, ConversationRegistry
from .events import END_OF_TURN, EventEmitter
from .llm import LLMClient, LLMNotConfigured
from .loop import run_turn
from .tools import AgentTools

#: async (url) -> {"ok": bool, "status": Optional[int], "detail": str}
Verifier = Callable[[str], Awaitable[dict]]


async def default_verifier(url: str, timeout: float = 10.0) -> dict:
    """Fetch the deployed URL ('curl the env URL') and report reachability."""
    try:
        async with httpx.AsyncClient(timeout=timeout,
                                     follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.TimeoutException:
        return {"ok": False, "status": None, "detail": "timed out"}
    except httpx.HTTPError as exc:
        return {"ok": False, "status": None, "detail": str(exc)[:200]}
    ok = 200 <= response.status_code < 400
    return {"ok": ok, "status": response.status_code,
            "detail": "" if ok else f"HTTP {response.status_code}"}


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


def create_app(settings: Optional[Settings] = None, *,
               llm_factory: Optional[Callable[[], Any]] = None,
               transport: Optional[httpx.AsyncBaseTransport] = None,
               verifier: Optional[Verifier] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    app = FastAPI(title="VAP Copilot agent", version="0.1.0",
                  docs_url=None, redoc_url=None)
    registry = ConversationRegistry()
    app.state.settings = settings
    app.state.registry = registry
    app.state.llm_factory = llm_factory
    app.state.transport = transport
    app.state.verifier = verifier

    def _make_llm() -> Any:
        if llm_factory is not None:
            return llm_factory()
        return LLMClient.from_settings(settings)

    async def _make_verifier() -> Verifier:
        if verifier is not None:
            return verifier
        async def _verify(url: str) -> dict:
            return await default_verifier(url, settings.verify_timeout)
        return _verify

    # -- SSE turn machinery ------------------------------------------------

    async def _event_stream(queue: "asyncio.Queue") -> AsyncIterator[str]:
        while True:
            try:
                item = await asyncio.wait_for(
                    queue.get(), timeout=settings.keepalive_seconds)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is END_OF_TURN:
                break
            yield item.encode()

    @app.post("/api/chat")
    async def chat(request: Request):
        try:
            auth = AuthConfig.from_headers(request.headers)
        except NoCredentialsError as exc:
            return _error("no_credentials", str(exc), 401)
        if not settings.llm_configured and llm_factory is None:
            return _error("llm_not_configured",
                          "no LLM endpoint configured: set VAP_LLM_BASE_URL "
                          "and VAP_LLM_MODEL", 503)

        try:
            body = await request.json()
        except ValueError:
            return _error("bad_request", "request body must be JSON", 400)
        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(message, str) or not message.strip():
            return _error("bad_request",
                          "field [message] must be a non-empty string", 400)
        conversation_id = body.get("conversation_id")
        if conversation_id is not None and not isinstance(conversation_id,
                                                          str):
            return _error("bad_request",
                          "field [conversation_id] must be a string or null",
                          400)

        context = {}
        raw_context = request.headers.get("x-vap-context")
        if raw_context:
            try:
                parsed = json.loads(raw_context)
            except ValueError:
                return _error("bad_request",
                              "header X-Vap-Context must be JSON", 400)
            if not isinstance(parsed, dict):
                return _error("bad_request",
                              "header X-Vap-Context must be a JSON object",
                              400)
            context = parsed

        conv = registry.get_or_create(conversation_id)
        conv.supersede()               # a new message takes over the turn
        if context:
            conv.context = context

        queue: asyncio.Queue = asyncio.Queue()
        conv.begin_turn(queue)
        emitter = EventEmitter(queue)
        tools = AgentTools(auth, emitter, conv,
                           transport=transport,
                           timeout=settings.tool_timeout,
                           poll_interval=settings.poll_interval,
                           verifier=await _make_verifier())

        async def _run() -> None:
            llm = None
            try:
                llm = _make_llm()
                await run_turn(conversation=conv, emitter=emitter,
                               tools=tools, llm=llm,
                               user_message=message.strip(),
                               settings=settings)
            except LLMNotConfigured as exc:
                await emitter.error("llm_not_configured", str(exc))
            finally:
                if llm is not None and hasattr(llm, "aclose"):
                    try:
                        await llm.aclose()
                    except Exception:
                        pass
                await queue.put(END_OF_TURN)

        conv.turn_task = asyncio.create_task(_run())
        return StreamingResponse(
            _event_stream(queue),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no",
                     "X-Conversation-Id": conv.id})

    @app.post("/api/answer")
    async def answer(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return _error("bad_request", "request body must be JSON", 400)
        if not isinstance(body, dict):
            return _error("bad_request", "request body must be an object",
                          400)
        conv = registry.get(body.get("conversation_id"))
        if conv is None:
            return _error("bad_request",
                          "unknown conversation_id (or turn already ended)",
                          400)
        try:
            if "ask_id" in body:
                kind = conv.resolve(ask_id=body.get("ask_id"),
                                    answer=str(body.get("answer") or ""))
            elif "confirm_id" in body:
                kind = conv.resolve(confirm_id=body.get("confirm_id"),
                                    approved=bool(body.get("approved")))
            elif "action" in body:
                kind = conv.resolve(action=str(body.get("action") or ""),
                                    payload=body.get("payload")
                                    if isinstance(body.get("payload"), dict)
                                    else None)
            else:
                return _error("bad_request",
                              "body must carry [ask_id], [confirm_id] or "
                              "[action]", 400)
        except AnswerMismatch as exc:
            return _error("bad_request", str(exc), 400)
        return {"ok": True, "kind": kind}

    @app.get("/api/context")
    async def get_context(request: Request):
        conv = registry.get(request.query_params.get("conversation_id"))
        context = dict(conv.context) if conv is not None else {}
        return {"conversation_id": conv.id if conv is not None else None,
                "context": context}

    @app.get("/api/audit")
    async def get_audit(request: Request):
        conv = registry.get(request.query_params.get("conversation_id"))
        if conv is None:
            return []
        return conv.audit_entries()

    @app.get("/healthz")
    async def healthz():
        return settings.healthz()

    return app


#: ASGI entry point (uvicorn / deploy/serve.py)
app = create_app()
