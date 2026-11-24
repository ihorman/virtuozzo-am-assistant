"""SSE event model for the extension <-> agent contract (v1.1).

Wire encoding: each event is an SSE message with the event name in the SSE
``event:`` field and the JSON payload in ``data:`` — exactly the payload
objects of docs/API-CONTRACT.md:

    event: token
    data: {"text": "..."}

    event: ask_user
    data: {"question": "...", "options": ["..."], "allow_free": true,
           "ask_id": "..."}

Keepalives during a paused turn are SSE comments: ``: keepalive``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Optional

#: event name -> payload keys allowed (docs/API-CONTRACT.md table)
EVENT_SCHEMA: dict[str, tuple[str, ...]] = {
    "token": ("text",),
    "tool_call": ("name", "args", "call_id"),
    "tool_result": ("call_id", "ok", "summary"),
    "ask_user": ("question", "options", "allow_free", "ask_id"),
    "confirm_action": ("summary", "actions", "confirm_id", "danger"),
    "progress": ("label", "call_id"),
    "card": ("kind", "title", "body_md", "actions"),
    "done": ("conversation_id",),
    "error": ("code", "message"),
}

#: card kinds (docs/API-CONTRACT.md `card` row)
CARD_KINDS = ("success", "info", "error", "security")

#: card action verbs (docs/API-CONTRACT.md `card` row)
CARD_ACTIONS = ("auto_fix", "retry", "open_logs", "open_url", "custom")

#: end-of-stream sentinel on the turn queue
END_OF_TURN = object()


@dataclass(frozen=True)
class Event:
    name: str
    payload: dict

    def encode(self) -> str:
        return (f"event: {self.name}\n"
                f"data: {json.dumps(self.payload, ensure_ascii=False)}\n\n")


def chunk_text(text: str, size: int = 400) -> list[str]:
    """Split narration into token-sized chunks (streamed feel)."""
    if len(text) <= size:
        return [text] if text else []
    chunks = []
    rest = text
    while rest:
        if len(rest) <= size:
            chunks.append(rest)
            break
        cut = rest.rfind(" ", 0, size)
        if cut <= 0:
            cut = size
        chunks.append(rest[:cut])
        rest = rest[cut:].lstrip()
    return chunks


class EventEmitter:
    """Pushes contract events onto the per-turn SSE queue."""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue
        self.terminal_seen = False

    async def emit(self, name: str, payload: Optional[dict] = None) -> Event:
        if name not in EVENT_SCHEMA:
            raise ValueError(f"unknown SSE event [{name}]")
        event = Event(name, payload or {})
        await self._queue.put(event)
        return event

    async def text(self, text: str) -> None:
        for chunk in chunk_text(text):
            await self.emit("token", {"text": chunk})

    async def done(self, conversation_id: str) -> None:
        self.terminal_seen = True
        await self.emit("done", {"conversation_id": conversation_id})

    async def error(self, code: str, message: str) -> None:
        self.terminal_seen = True
        await self.emit("error", {"code": code, "message": message})
