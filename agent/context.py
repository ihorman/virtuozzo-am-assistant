"""Shared turn context for guided flows, diagnosis and security review.

:class:`FlowContext` is the only surface the flows use to talk to the user
and the platform. It encodes the product rules in one place:

- ONE clarifying question at a time (``ask`` — ``ask_user`` event + pause);
- no mutation without an approved ``confirm_action`` (``confirm`` is the
  only way flows get ``confirmed=True`` tool runs);
- rich results as cards (``card``), one-click fixes as card actions
  (``wait_card_action`` — same pause mechanism as ask/confirm);
- narration as ``token`` events (``say``).
"""

from __future__ import annotations

import uuid
from typing import Callable, Optional, Sequence

from .conversation import Conversation, TurnSuperseded
from .events import CARD_ACTIONS, CARD_KINDS, EventEmitter
from .tools import AgentTools, ToolOutcome


def match_option(answer: str, options: Sequence[str]) -> str:
    """Map a free-text answer onto an option, or return it verbatim.

    Exact (case-insensitive) match wins; then a substring match either way
    ("prod" -> "Prod - 12 cloudlets"); otherwise the raw text is the value.
    """
    text = (answer or "").strip()
    lowered = text.lower()
    for option in options:
        if option.lower() == lowered:
            return option
    for option in options:
        opt = option.lower()
        if lowered and (lowered in opt or opt in lowered):
            return option
    return text


class FlowContext:
    """Turn-scoped toolkit for one guided flow / diagnosis run."""

    def __init__(self, emitter: EventEmitter, tools: AgentTools,
                 conversation: Conversation, params: Optional[dict] = None):
        self.emitter = emitter
        self.tools = tools
        self.conversation = conversation
        self.params = dict(params or {})

    # -- narration ----------------------------------------------------------

    async def say(self, text: str) -> None:
        await self.emitter.text(text)

    # -- clarifying question (ask_user + pause) -----------------------------

    async def ask(self, question: str, options: Sequence[str] = (),
                  allow_free: bool = True,
                  validate: Optional[Callable[[str], Optional[str]]] = None
                  ) -> str:
        """Ask ONE question; returns the (option-matched) answer.

        ``validate`` maps an invalid answer to a re-ask hint; the question is
        repeated until the answer passes (bounded to avoid a stuck loop).
        """
        options = list(options)
        for _ in range(5):
            ask_id = "ask-" + uuid.uuid4().hex[:10]
            await self.emitter.emit("ask_user", {
                "question": question,
                "options": options,
                "allow_free": bool(allow_free),
                "ask_id": ask_id,
            })
            raw = await self.conversation.wait("ask", ask_id,
                                               options=tuple(options))
            answer = match_option(str(raw), options)
            if validate is None:
                return answer
            hint = validate(answer)
            if hint is None:
                return answer
            question = hint
        return answer

    # -- mutation gate (confirm_action + pause) -----------------------------

    async def confirm(self, summary: str, actions: Sequence[str] = (),
                      danger: bool = False) -> bool:
        """Present the recap and wait for Confirm/Cancel."""
        confirm_id = "confirm-" + uuid.uuid4().hex[:10]
        await self.emitter.emit("confirm_action", {
            "summary": summary,
            "actions": list(actions),
            "confirm_id": confirm_id,
            "danger": bool(danger),
        })
        approved = await self.conversation.wait("confirm", confirm_id)
        return bool(approved)

    # -- cards --------------------------------------------------------------

    async def card(self, kind: str, title: str, body_md: str,
                   actions: Sequence[dict] = ()) -> None:
        if kind not in CARD_KINDS:
            raise ValueError(f"unknown card kind [{kind}]")
        normalized = []
        for action in actions:
            verb = action.get("action", "custom")
            if verb not in CARD_ACTIONS:
                verb = "custom"
            normalized.append({
                "label": str(action.get("label", "")),
                "url": str(action.get("url", "")),
                "action": verb,
                "payload": dict(action.get("payload") or {}),
            })
        await self.emitter.emit("card", {
            "kind": kind, "title": title, "body_md": body_md,
            "actions": normalized,
        })

    async def wait_card_action(self, actions: Sequence[dict]
                               ) -> Optional[dict]:
        """Pause until the user clicks a card action.

        Returns ``{"action": ..., "payload": ...}`` or ``None`` when the wait
        was superseded (a newer chat message took over).
        """
        try:
            result = await self.conversation.wait(
                "card_action", "card-" + uuid.uuid4().hex[:10],
                options=tuple(a.get("action", "") for a in actions))
        except TurnSuperseded:
            return None
        if not isinstance(result, dict):
            return None
        if result.get("action") in (None, "", "cancel"):
            return None
        return result

    # -- platform -----------------------------------------------------------

    async def run_tool(self, name: str, args: Optional[dict] = None, *,
                       confirmed: bool = False,
                       progress: Optional[str] = None) -> ToolOutcome:
        return await self.tools.run(name, args, confirmed=confirmed,
                                    progress=progress)

    async def verify_url(self, url: str) -> ToolOutcome:
        return await self.tools.verify_url(url)
