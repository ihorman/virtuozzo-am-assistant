"""The LLM-driven turn loop (docs/PLAN.md section 3).

One turn = one user message. The model replies through the *directive
protocol* (see :mod:`agent.prompts`): narration is streamed as ``token``
events, and at most one fenced ``vap`` JSON block per reply carries the
action (``start_flow`` / ``ask_user`` / ``call_tool`` / ``finish``).

The loop is deterministic where it matters: guided flows are state machines
(:mod:`agent.flows`), mutations go through ``confirm_action`` gates, and any
failed step goes through error -> diagnose -> auto-fix instead of dead-
ending. ``done`` is emitted exactly once, last, on success; ``error`` ends
the turn instead on failure; a superseded turn ends silently (the newer
message owns the stream).
"""

from __future__ import annotations

from typing import Any, List, Optional

from .config import Settings
from .context import FlowContext
from .conversation import Conversation, TurnSuperseded
from .events import EventEmitter
from .flows import dispatch, handle_failure
from .llm import LLMError
from .prompts import SYSTEM_PROMPT, DirectiveError, parse_directive
from .tools import AgentTools, ConfirmationRequired, ToolOutcome
from .tools_util import mask_args

from mcp.tools import TOOLS

#: safety valve: never let one turn run more LLM rounds than this
MAX_ROUNDS = 8


def _tool_summary(outcome: ToolOutcome) -> str:
    return f"tool [{outcome.name}] -> {'ok' if outcome.ok else 'FAILED'}: " \
           f"{outcome.summary}"


async def run_confirmed_tool(ctx: FlowContext, name: str,
                             args: Optional[dict] = None) -> Optional[
                                 ToolOutcome]:
    """Run one direct ``call_tool`` directive.

    Read-only tools run at once. Mutating tools are gated behind an approved
    ``confirm_action`` for exactly this action (contract security notes) —
    the confirmation is what makes ``confirmed=True`` legal.
    """
    args = dict(args or {})
    spec = TOOLS.get(name)
    if spec is not None and spec.mutating:
        recap = f"I am about to run **[{name}]** with " \
                f"`{mask_args(name, args)}`. This changes the platform."
        approved = await ctx.confirm(recap, actions=[name],
                                     danger=(name == "env_delete"))
        if not approved:
            await ctx.say("Cancelled — I changed nothing.")
            return None
    outcome = await ctx.run_tool(name, args,
                                 confirmed=(spec is not None and
                                            spec.mutating))
    return outcome


async def run_turn(*, conversation: Conversation, emitter: EventEmitter,
                   tools: AgentTools, llm: Any, user_message: str,
                   settings: Settings) -> None:
    """Drive one chat turn to its terminal event. Never raises."""
    ctx = FlowContext(emitter, tools, conversation)
    history: List[dict] = conversation.history
    history.append({"role": "user", "content": user_message})
    try:
        for _ in range(MAX_ROUNDS):
            reply = await llm.complete(
                [{"role": "system", "content": SYSTEM_PROMPT}] + history)
            history.append({"role": "assistant", "content": reply})

            try:
                narration, directive = parse_directive(reply)
            except DirectiveError as exc:
                await emitter.error(
                    "invalid_directive",
                    "The assistant produced an unreadable action — please "
                    "repeat your request.")
                return
            if narration:
                await emitter.text(narration)

            if directive is None or directive.get("action") == "finish":
                break

            action = directive.get("action")
            if action == "start_flow":
                flow = str(directive.get("flow") or "")
                params = directive.get("params") or {}
                if not isinstance(params, dict):
                    params = {}
                started = await dispatch(ctx, flow, params)
                if not started:
                    await emitter.error(
                        "bad_request", f"unknown guided flow [{flow}]")
                    return
                break

            if action == "ask_user":
                answer = await ctx.ask(
                    str(directive.get("question") or "..."),
                    [str(o) for o in (directive.get("options") or [])],
                    allow_free=bool(directive.get("allow_free", True)))
                history.append({"role": "user", "content": answer})
                continue

            if action == "call_tool":
                name = str(directive.get("name") or "")
                args = directive.get("args") or {}
                if not isinstance(args, dict):
                    args = {}
                try:
                    outcome = await run_confirmed_tool(ctx, name, args)
                except ConfirmationRequired:
                    await emitter.error(
                        "confirm_required",
                        f"[{name}] changes the platform and was refused: "
                        f"it needs an explicit confirmation.")
                    return
                if outcome is None:     # confirmation declined
                    continue
                history.append({"role": "user",
                                "content": _tool_summary(outcome)})
                if not outcome.ok:
                    await handle_failure(ctx, outcome, step_args=args,
                                         retry_tool=name)
                    break
                continue

            # unknown directive action: recoverable, ends the turn cleanly
            await emitter.error(
                "bad_request", f"unknown action [{action}]")
            return

        await emitter.done(conversation.id)
    except TurnSuperseded:
        return                      # a newer message owns the stream
    except LLMError as exc:
        code = "llm_not_configured" if "not configured" in str(exc) \
            else "llm_error"
        await emitter.error(code, str(exc))
    except ConfirmationRequired as exc:      # defensive
        await emitter.error("confirm_required", str(exc))
    except Exception as exc:                 # never crash the stream
        await emitter.error("internal_error", f"unexpected agent error: "
                                              f"{type(exc).__name__}")
