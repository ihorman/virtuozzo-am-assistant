"""Tool execution for the agent: events, masking, summaries, audit.

Wraps :func:`mcp.tools.execute_tool` (which carries its own safety gates:
strict ``confirm=true`` on every mutating tool, password gate on
``env_delete``) and adds the agent-side contract:

- emits ``tool_call`` -> optional ``progress`` -> ``tool_result`` with a
  **summary only** — raw JSON payloads never reach the user;
- masks passwords / secret values in tool arguments and summaries;
- enforces a second confirmation gate: a mutating tool is refused unless the
  flow explicitly declares it confirmed (i.e. an approved
  ``confirm_action`` preceded it);
- records every mutation in the conversation audit trail.

The deploy-verify step ("curl the env URL") is here as :meth:`verify_url`,
emitted as a ``verify_http`` tool call so the UI shows it like any step.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .conversation import Conversation
from .events import EventEmitter
from .tools_util import mask_args, mask_value, summarize
from mcp.tools import TOOLS, AuthConfig, execute_tool

#: async (url) -> {"ok": bool, "status": Optional[int], "detail": str}
Verifier = Callable[[str], Awaitable[dict]]


class ConfirmationRequired(Exception):
    """Internal gate: a mutating tool ran without an approved confirmation."""


@dataclass
class ToolOutcome:
    name: str
    call_id: str
    payload: dict
    ok: bool
    summary: str


class AgentTools:
    """Executes MCP tools on behalf of one conversation turn."""

    def __init__(self, auth: AuthConfig, emitter: EventEmitter,
                 conversation: Conversation, *,
                 transport=None, timeout: float = 30.0,
                 poll_interval: float = 0.25,
                 verifier: Optional[Verifier] = None):
        self.auth = auth
        self.emitter = emitter
        self.conversation = conversation
        self.transport = transport
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.verifier = verifier

    # -- platform tools -----------------------------------------------------

    async def run(self, name: str, args: Optional[dict] = None, *,
                  confirmed: bool = False,
                  progress: Optional[str] = None) -> ToolOutcome:
        """Run one MCP tool with full event / audit plumbing.

        ``confirmed=True`` is required for mutating tools and MUST only be
        passed after an approved ``confirm_action`` for exactly this action.
        """
        args = dict(args or {})
        spec = TOOLS.get(name)
        if spec is not None and spec.mutating:
            if not confirmed:
                raise ConfirmationRequired(
                    f"[{name}] is a mutating tool: an approved "
                    f"confirm_action is required before it can run")
            args.setdefault("confirm", True)

        call_id = uuid.uuid4().hex[:12]
        await self.emitter.emit("tool_call", {
            "name": name, "args": mask_args(name, args), "call_id": call_id})
        if progress:
            await self.emitter.emit("progress", {"label": progress,
                                                 "call_id": call_id})

        payload, is_error = await execute_tool(
            name, args, self.auth, transport=self.transport,
            timeout=self.timeout, poll_interval=self.poll_interval)
        ok = not is_error

        summary = summarize(name, args, payload, ok)
        await self.emitter.emit("tool_result", {
            "call_id": call_id, "ok": ok, "summary": summary})

        if spec is not None and spec.mutating:
            self.conversation.record_audit(
                tool=name, target=str(args.get("env_name") or "-"),
                summary=summary, ok=ok)
        return ToolOutcome(name=name, call_id=call_id, payload=payload,
                           ok=ok, summary=summary)

    # -- deploy verification ("curl the env URL") ---------------------------

    async def verify_url(self, url: str) -> ToolOutcome:
        """Fetch the deployed application URL and report reachability."""
        call_id = uuid.uuid4().hex[:12]
        await self.emitter.emit("tool_call", {
            "name": "verify_http", "args": {"url": url}, "call_id": call_id})
        await self.emitter.emit("progress", {
            "label": f"Verifying {url}", "call_id": call_id})
        if self.verifier is None:
            result = {"ok": False, "status": None,
                      "detail": "no verifier configured"}
        else:
            try:
                result = await self.verifier(url)
            except Exception as exc:  # verifier must never crash a turn
                result = {"ok": False, "status": None, "detail": str(exc)[:200]}
        ok = bool(result.get("ok"))
        status = result.get("status")
        detail = str(result.get("detail") or "")
        if ok:
            summary = f"{url} is reachable (HTTP {status})"
        else:
            summary = (f"{url} did not respond successfully"
                       + (f" (HTTP {status})" if status else "")
                       + (f": {detail}" if detail else ""))
        await self.emitter.emit("tool_result", {
            "call_id": call_id, "ok": ok, "summary": summary})
        return ToolOutcome(name="verify_http", call_id=call_id,
                           payload=dict(result), ok=ok, summary=summary)
