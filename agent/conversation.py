"""Per-conversation state: history, pauses, audit trail, repair plans.

The contract's pause semantics ("after ``ask_user``/``confirm_action`` the
stream PAUSES until ``POST /api/answer``") are implemented as a *pending
wait*: the turn parks on an :class:`asyncio.Future`, ``/api/answer``
resolves it, and the turn continues in the same SSE stream.

The same mechanism serves rich cards: an error card offering ``auto_fix`` or
a security card offering one-click fixes pauses until the user picks an
action (``POST /api/answer`` with ``action`` + ``payload`` — the additive
v1.2 shape documented in agent/README.md).

Nothing here stores credentials: the platform session token lives only in
the request headers of the current turn, passwords are passed through and
dropped, and audit summaries are masked.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class TurnSuperseded(Exception):
    """A newer chat message arrived; the parked turn must end gracefully."""


class AnswerMismatch(Exception):
    """``/api/answer`` targeted the wrong pending item."""


@dataclass
class PendingWait:
    """One parked ask_user / confirm_action / card_action."""
    kind: str                # "ask" | "confirm" | "card_action"
    ident: str               # ask_id / confirm_id / card action correlation
    future: asyncio.Future
    options: tuple = ()
    meta: dict = field(default_factory=dict)


@dataclass
class AuditEntry:
    """One platform mutation (compliance record; no secret values)."""
    at: float
    tool: str
    target: str
    summary: str
    ok: bool

    def to_dict(self) -> dict:
        return {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.at)),
                "tool": self.tool, "target": self.target,
                "summary": self.summary, "ok": self.ok}


class Conversation:
    """Everything one chat thread needs between turns and during pauses."""

    def __init__(self, conversation_id: Optional[str] = None):
        self.id = conversation_id or uuid.uuid4().hex
        self.history: List[dict] = []          # LLM messages (no secrets)
        self.context: dict = {}                # last X-Vap-Context
        self.audit: List[AuditEntry] = []
        self.repair_plans: Dict[str, dict] = {}
        self.pending: Optional[PendingWait] = None
        self.turn_task: Optional[asyncio.Task] = None
        self.events: Optional[asyncio.Queue] = None

    # -- turn lifecycle -----------------------------------------------------

    def begin_turn(self, queue: asyncio.Queue) -> None:
        self.events = queue

    def supersede(self) -> None:
        """Unblock a parked turn: a new user message takes over."""
        if self.pending is not None and not self.pending.future.done():
            self.pending.future.set_exception(TurnSuperseded())
        if self.turn_task is not None and not self.turn_task.done():
            self.turn_task.cancel()

    # -- pause protocol -----------------------------------------------------

    async def wait(self, kind: str, ident: str, *,
                   options: tuple = (), meta: Optional[dict] = None) -> Any:
        """Park the turn until :meth:`resolve` (via ``/api/answer``)."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self.pending = PendingWait(kind=kind, ident=ident, future=future,
                                   options=options, meta=meta or {})
        try:
            return await future
        finally:
            self.pending = None

    def resolve(self, *, ask_id: Optional[str] = None,
                confirm_id: Optional[str] = None,
                action: Optional[str] = None,
                payload: Optional[dict] = None,
                answer: Optional[str] = None,
                approved: Optional[bool] = None) -> str:
        """Resolve the pending wait. Raises :class:`AnswerMismatch` when the
        message does not target the pending item."""
        pending = self.pending
        if pending is None:
            raise AnswerMismatch("no question is waiting for an answer")
        if pending.kind == "ask":
            if ask_id != pending.ident:
                raise AnswerMismatch("ask_id does not match the pending "
                                     "question")
            pending.future.set_result(str(answer if answer is not None else ""))
        elif pending.kind == "confirm":
            if confirm_id != pending.ident:
                raise AnswerMismatch("confirm_id does not match the pending "
                                     "confirmation")
            pending.future.set_result(bool(approved))
        elif pending.kind == "card_action":
            pending.future.set_result({"action": action or "cancel",
                                       "payload": payload or {}})
        else:  # pragma: no cover - defensive
            raise AnswerMismatch(f"unknown pending kind [{pending.kind}]")
        return pending.kind

    # -- audit trail (PLAN section 6: every mutation, no secrets) -----------

    def record_audit(self, tool: str, target: str, summary: str,
                     ok: bool) -> None:
        self.audit.append(AuditEntry(at=time.time(), tool=tool, target=target,
                                     summary=summary, ok=ok))

    def audit_entries(self) -> List[dict]:
        return [entry.to_dict() for entry in self.audit]

    # -- repair plans (error -> diagnose -> auto-fix) -----------------------

    def register_repair_plan(self, plan: dict) -> str:
        plan_id = "repair-" + uuid.uuid4().hex[:10]
        self.repair_plans[plan_id] = plan
        return plan_id

    def get_repair_plan(self, plan_id: Optional[str]) -> Optional[dict]:
        if not plan_id:
            return None
        return self.repair_plans.get(plan_id)


class ConversationRegistry:
    """In-memory conversation store (per-process; nothing persisted)."""

    def __init__(self) -> None:
        self._conversations: Dict[str, Conversation] = {}

    def get_or_create(self, conversation_id: Optional[str]) -> Conversation:
        if conversation_id:
            conv = self._conversations.get(conversation_id)
            if conv is not None:
                return conv
            conv = Conversation(conversation_id)
            self._conversations[conv.id] = conv
            return conv
        conv = Conversation()
        self._conversations[conv.id] = conv
        return conv

    def get(self, conversation_id: Optional[str]) -> Optional[Conversation]:
        if not conversation_id:
            return None
        return self._conversations.get(conversation_id)
