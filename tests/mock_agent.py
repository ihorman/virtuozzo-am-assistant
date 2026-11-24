"""Scripted mock agent for the extension smoke tests (and extension dev).

Speaks the frozen extension<->agent contract (docs/API-CONTRACT.md v1.1/1.2)
over real sockets, but every reply is a deterministic scenario keyed on the
message text — no LLM, no platform. Scenarios cover every UI state the
sidepanel must render (docs/DESIGN.md component inventory):

- ``deploy``    -> ask_user -> confirm_action -> progress -> success card
- ``database``  -> ask_user -> confirm_action -> progress -> success card
- ``security``  -> security card with severity findings + one-click fixes
- ``error``     -> error card with ``auto_fix`` -> confirm -> progress -> success
- ``slow``      -> long progress card (elapsed-timer screenshots)
- ``bill`` / ``faster`` -> info cards
- anything else -> a friendly markdown reply

Run standalone for extension development::

    python3 -m tests.mock_agent --port 8787
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import uuid
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

END_OF_TURN = object()

KEEPALIVE_SECONDS = 15.0

#: scenario pauses are scaled by this (tests stretch it to screenshot
#: in-flight states like the progress card; 1.0 = real-world pacing)
PAUSE_SCALE = float(os.environ.get("VAP_MOCK_PAUSE_SCALE", "1.0"))


def _event(name: str, payload: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


class Conversation:
    """One conversation: a turn queue plus at most one pending wait."""

    def __init__(self) -> None:
        self.id = str(uuid.uuid4())
        self.queue: asyncio.Queue = asyncio.Queue()
        self.pending: Optional[dict] = None      # {"kind", "id"}
        self.resolution: Optional[dict] = None
        self.resolved = asyncio.Event()
        self.audit: list = []

    async def emit(self, name: str, payload: dict) -> None:
        await self.queue.put(_event(name, payload))

    async def wait_for_answer(self, kind: str, wait_id: str) -> dict:
        """Pause the turn until POST /api/answer resolves this wait."""
        self.pending = {"kind": kind, "id": wait_id}
        self.resolved.clear()
        await self.resolved.wait()
        self.pending = None
        return self.resolution or {}

    def resolve(self, resolution: dict) -> str:
        self.resolution = resolution
        self.resolved.set()
        return self.pending["kind"] if self.pending else "none"

    def record(self, tool: str, target: str, summary: str, ok: bool = True) -> None:
        self.audit.append({
            "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "tool": tool,
            "target": target,
            "summary": summary,
            "ok": ok,
        })


# --------------------------------------------------------------------- #
# scenarios                                                             #
# --------------------------------------------------------------------- #

async def _run_deploy(conv: Conversation, message: str) -> None:
    await conv.emit("token", {"text": "Great — let's get your first app online. "
                                     "Two quick questions and I'll handle the rest."})
    await conv.emit("ask_user", {
        "question": "What should we deploy?",
        "options": ["Node.js + PostgreSQL", "PHP + MySQL", "Static site"],
        "allow_free": True,
        "ask_id": "ask-1",
    })
    answer = await conv.wait_for_answer("ask", "ask-1")
    choice = answer.get("answer", "Node.js + PostgreSQL")
    await conv.emit("token", {"text": f"Nice — **{choice}**. Here is what I'll create:"})
    await conv.emit("confirm_action", {
        "summary": "Creates 1 environment with Node.js 20 + PostgreSQL, ~$12/mo est.",
        "actions": [
            "Create environment `my-demo-env` (4 cloudlets, Frankfurt)",
            "Attach PostgreSQL 16 database node",
            "Enable Let's Encrypt SSL for my-demo-env.example.com",
        ],
        "confirm_id": "confirm-1",
        "danger": False,
    })
    confirmation = await conv.wait_for_answer("confirm", "confirm-1")
    if not confirmation.get("approved"):
        await conv.emit("token", {"text": "Cancelled — nothing was created."})
        await conv.emit("done", {"conversation_id": conv.id})
        return

    await conv.emit("tool_call", {"name": "env_create", "args": {"envName": "my-demo-env"},
                                  "call_id": "call-1"})
    await conv.emit("progress", {"label": "Creating environment my-demo-env…",
                                 "call_id": "call-1"})
    await asyncio.sleep(1.2 * PAUSE_SCALE)
    conv.record("env_create", "my-demo-env", "created environment (Node.js 20 + PostgreSQL)")
    await conv.emit("tool_result", {"call_id": "call-1", "ok": True,
                                    "summary": "environment created"})
    await conv.emit("card", {
        "kind": "success",
        "title": "my-demo-env is live",
        "body_md": (
            "Deployed **Node.js 20 + PostgreSQL 16** in Frankfurt.\n\n"
            "App URL:\n\n"
            "```\nhttps://my-demo-env.example.com\n```\n\n"
            "Database (password shown once):\n\n"
            "```\npostgres://app:7hK2-xQ9m@my-demo-env-db:5432/app\n```"
        ),
        "actions": [
            {"label": "Open app ↗", "action": "open_url",
             "url": "https://my-demo-env.example.com"},
            {"label": "Open dashboard ↗", "action": "open_url",
             "url": "https://app.example.com/env/my-demo-env"},
            {"label": "View logs", "action": "open_logs"},
        ],
    })
    await conv.emit("done", {"conversation_id": conv.id})


async def _run_database(conv: Conversation, message: str) -> None:
    await conv.emit("token", {"text": "Sure — a database node is a few clicks."})
    await conv.emit("ask_user", {
        "question": "Which database would you like?",
        "options": ["PostgreSQL 16", "MySQL 8", "MariaDB 10"],
        "allow_free": True,
        "ask_id": "ask-1",
    })
    answer = await conv.wait_for_answer("ask", "ask-1")
    choice = answer.get("answer", "PostgreSQL 16")
    await conv.emit("confirm_action", {
        "summary": f"Adds a {choice} node to my-demo-env (2 cloudlets, ~$8/mo est.).",
        "actions": [f"Attach {choice} database node", "Restart application nodes"],
        "confirm_id": "confirm-1",
        "danger": False,
    })
    confirmation = await conv.wait_for_answer("confirm", "confirm-1")
    if not confirmation.get("approved"):
        await conv.emit("token", {"text": "Cancelled — no changes made."})
        await conv.emit("done", {"conversation_id": conv.id})
        return
    await conv.emit("progress", {"label": f"Attaching {choice}…", "call_id": "call-1"})
    await asyncio.sleep(1.0 * PAUSE_SCALE)
    conv.record("db_add", "my-demo-env", f"attached {choice} database node")
    await conv.emit("card", {
        "kind": "success",
        "title": f"{choice} is attached",
        "body_md": "Connection string (shown once):\n\n"
                   "```\npostgres://app:R4t-8Wq1@my-demo-env-db:5432/app\n```",
        "actions": [{"label": "Open dashboard ↗", "action": "open_url",
                     "url": "https://app.example.com/env/my-demo-env"},
                    {"label": "View logs", "action": "open_logs"}],
    })
    await conv.emit("done", {"conversation_id": conv.id})


async def _run_security(conv: Conversation, message: str) -> None:
    await conv.emit("token", {"text": "Running a security review on **my-demo-env**…"})
    await asyncio.sleep(0.3)
    await conv.emit("card", {
        "kind": "security",
        "title": "Security review — 3 findings",
        "body_md": (
            "- **high** — Public SSH port (22) is open to `0.0.0.0/0`\n"
            "- **medium** — Let's Encrypt certificate expires in 12 days\n"
            "- **low** — Environment variables contain 1 unmasked-looking secret"
        ),
        "actions": [
            {"label": "Fix high severity", "action": "auto_fix",
             "payload": {"repair_plan_id": "repair-ssh"}},
            {"label": "Fix all", "action": "auto_fix",
             "payload": {"repair_plan_id": "repair-all"}},
        ],
    })
    await conv.emit("done", {"conversation_id": conv.id})


async def _run_error(conv: Conversation, message: str) -> None:
    await conv.emit("token", {"text": "The deploy started but the build failed — "
                                     "let me look at the logs first."})
    await conv.emit("tool_call", {"name": "env_getlogs", "args": {"envName": "my-demo-env"},
                                  "call_id": "call-1"})
    await conv.emit("tool_result", {"call_id": "call-1", "ok": True,
                                    "summary": "read build log (last 50 lines)"})
    await conv.emit("card", {
        "kind": "error",
        "title": "The build ran out of memory",
        "body_md": (
            "The build container hit its **512 MB** memory limit while installing "
            "dependencies. This is a sizing problem, not a code problem.\n\n"
            "> FATAL: heap out of memory (npm install)"
        ),
        "actions": [
            {"label": "Fix it automatically", "action": "auto_fix",
             "payload": {"repair_plan_id": "repair-mem"}},
            {"label": "Retry", "action": "retry"},
            {"label": "View logs", "action": "open_logs"},
        ],
    })
    action = await conv.wait_for_answer("action", "card-1")
    if action.get("action") == "auto_fix":
        await conv.emit("confirm_action", {
            "summary": "Doubles the build container memory to 1 GB and re-runs the build. "
                       "Cost impact: none (build-time only).",
            "actions": ["Set BUILD_MEMORY=1G", "Re-run build"],
            "confirm_id": "confirm-1",
            "danger": False,
        })
        confirmation = await conv.wait_for_answer("confirm", "confirm-1")
        if not confirmation.get("approved"):
            await conv.emit("token", {"text": "Fix cancelled."})
            await conv.emit("done", {"conversation_id": conv.id})
            return
    await conv.emit("progress", {"label": "Re-running the build…", "call_id": "call-2"})
    await asyncio.sleep(1.0 * PAUSE_SCALE)
    conv.record("env_var_add", "my-demo-env", "set BUILD_MEMORY=1G and rebuilt", ok=True)
    await conv.emit("card", {
        "kind": "success",
        "title": "Fixed — build is green",
        "body_md": "The build completed in **1m 42s** and the app is serving traffic.",
        "actions": [{"label": "Open app ↗", "action": "open_url",
                     "url": "https://my-demo-env.example.com"},
                    {"label": "View logs", "action": "open_logs"}],
    })
    await conv.emit("done", {"conversation_id": conv.id})


async def _run_slow(conv: Conversation, message: str) -> None:
    await conv.emit("token", {"text": "Starting a full backup — this takes a moment."})
    await conv.emit("progress", {"label": "Backing up my-demo-env (14 GB)…",
                                 "call_id": "call-1"})
    await asyncio.sleep(3.2 * PAUSE_SCALE)
    await conv.emit("card", {
        "kind": "success",
        "title": "Backup complete",
        "body_md": "Snapshot `backup-2026-11-24` stored in Frankfurt (14 GB).",
        "actions": [{"label": "Open dashboard ↗", "action": "open_url",
                     "url": "https://app.example.com/env/my-demo-env"}],
    })
    await conv.emit("done", {"conversation_id": conv.id})


async def _run_bill(conv: Conversation, message: str) -> None:
    await conv.emit("card", {
        "kind": "info",
        "title": "Your November bill",
        "body_md": (
            "This month so far: **$84.20** across 3 environments.\n\n"
            "- `my-demo-env` — $52.00 (4 cloudlets + database)\n"
            "- `staging-php` — $18.40 (sleeping 60% of the time)\n"
            "- `old-tests` — $13.80 (stopped, storage only)\n\n"
            "Tip: `old-tests` has been stopped for 21 days — deleting it saves "
            "`$13.80/mo`."
        ),
        "actions": [],
    })
    await conv.emit("done", {"conversation_id": conv.id})


async def _run_faster(conv: Conversation, message: str) -> None:
    await conv.emit("card", {
        "kind": "info",
        "title": "3 ways to make my-demo-env faster",
        "body_md": (
            "1. **Add Varnish cache** in front of Node.js — cuts TTFB ~60%\n"
            "2. **Enable CDN** for static assets\n"
            "3. **Scale web nodes to 2** — handles traffic spikes\n\n"
            "Say the word and I'll apply any of these behind a confirmation."
        ),
        "actions": [],
    })
    await conv.emit("done", {"conversation_id": conv.id})


async def _run_hello(conv: Conversation, message: str) -> None:
    await conv.emit("token", {
        "text": "Hello! I'm your **Application Management Assistant**. I can deploy apps, add databases, "
                "scale, troubleshoot and explain your bill.\n\n"
                "Try one of the suggestions below, or just tell me what you need.\n\n"
                "```\nDeploy my first app\n```"
    })
    await conv.emit("done", {"conversation_id": conv.id})


def pick_scenario(message: str):
    lowered = message.lower()
    if "deploy" in lowered:
        return _run_deploy
    if "database" in lowered or "db" in lowered.split():
        return _run_database
    if "security" in lowered or "secure" in lowered:
        return _run_security
    if "error" in lowered or "fail" in lowered:
        return _run_error
    if "slow" in lowered or "backup" in lowered:
        return _run_slow
    if "bill" in lowered:
        return _run_bill
    if "faster" in lowered or "speed" in lowered:
        return _run_faster
    return _run_hello


# --------------------------------------------------------------------- #
# FastAPI app                                                           #
# --------------------------------------------------------------------- #

def create_mock_agent() -> FastAPI:
    app = FastAPI(title="Application Management Assistant mock agent", docs_url=None, redoc_url=None)
    conversations: dict[str, Conversation] = {}

    def get_conv(conversation_id) -> Optional[Conversation]:
        return conversations.get(conversation_id) if conversation_id else None

    async def event_stream(conv: Conversation):
        while True:
            try:
                item = await asyncio.wait_for(conv.queue.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if item is END_OF_TURN:
                break
            yield item

    async def run_turn(conv: Conversation, message: str) -> None:
        try:
            await pick_scenario(message)(conv, message)
        finally:
            await conv.queue.put(END_OF_TURN)

    @app.post("/api/chat")
    async def chat(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"code": "bad_request", "message": "body must be JSON"},
                                status_code=400)
        message = body.get("message") if isinstance(body, dict) else None
        if not isinstance(message, str) or not message.strip():
            return JSONResponse(
                {"code": "bad_request", "message": "field [message] must be a non-empty string"},
                status_code=400)
        conv = get_conv(body.get("conversation_id"))
        if conv is None:
            conv = Conversation()
            conversations[conv.id] = conv
        asyncio.get_running_loop().create_task(run_turn(conv, message.strip()))
        return StreamingResponse(
            event_stream(conv),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache",
                     "X-Accel-Buffering": "no",
                     "X-Conversation-Id": conv.id})

    @app.post("/api/answer")
    async def answer(request: Request):
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"code": "bad_request", "message": "body must be JSON"},
                                status_code=400)
        conv = get_conv(body.get("conversation_id") if isinstance(body, dict) else None)
        if conv is None:
            return JSONResponse(
                {"code": "bad_request", "message": "unknown conversation_id"},
                status_code=400)
        if conv.pending is None:
            return JSONResponse(
                {"code": "bad_request", "message": "no pending question"},
                status_code=400)
        kind = conv.pending["kind"]
        pending_id = conv.pending["id"]
        if kind == "ask" and body.get("ask_id") != pending_id:
            return JSONResponse({"code": "bad_request", "message": "ask_id mismatch"},
                                status_code=400)
        if kind == "confirm" and body.get("confirm_id") != pending_id:
            return JSONResponse({"code": "bad_request", "message": "confirm_id mismatch"},
                                status_code=400)
        conv.resolve(dict(body))
        return {"ok": True, "kind": kind}

    @app.get("/api/context")
    async def context(request: Request):
        conv = get_conv(request.query_params.get("conversation_id"))
        return {"conversation_id": conv.id if conv else None,
                "context": {"envName": "my-demo-env", "page": "env"} if conv else {}}

    @app.get("/api/audit")
    async def audit(request: Request):
        conv = get_conv(request.query_params.get("conversation_id"))
        return conv.audit if conv else []

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "llm_configured": True, "version": "mock-agent-1"}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Application Management Assistant mock agent")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_mock_agent(), host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
