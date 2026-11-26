"""Error -> diagnose -> auto-fix (docs/PLAN.md section 3, flow 5).

On ANY failed tool step the agent must not dead-end. This module:

1. gathers evidence around the failure (env state, node stats, recent logs);
2. classifies the failure into a plain-language category;
3. builds an **auto-fix repair plan** (one-click ``auto_fix`` card action;
   the card action payload carries the repair plan id from
   :meth:`Conversation.register_repair_plan`).

Repair plans are built from fixed templates keyed by the failure class —
never from free-form model output — and every plan is re-checked by
:func:`repair_plan_is_safe` before it may run (PLAN section 3, flow 6
guardrails: a repair may never weaken security, never delete an environment,
never touch secrets). Unsafe plans are refused even if registered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from .context import FlowContext
from .tools import ToolOutcome
from .tools_util import slugify_env_name

#: tools a repair plan may never use (destructive / security-relevant).
#: ssl_letsencrypt is deliberately allowed: it strengthens security.
FORBIDDEN_REPAIR_TOOLS = {"env_delete", "env_var_set"}

#: argument keys that must never appear in a repair plan (secrets pass-through)
FORBIDDEN_REPAIR_ARGS = {"password", "passwd", "secret", "token", "api_key",
                         "session"}


@dataclass
class Diagnosis:
    """What went wrong, in human terms, and how it can be fixed."""

    category: str
    explanation: str
    evidence: List[dict] = field(default_factory=list)
    repair_plan: Optional[dict] = None   # registered plan -> id via registry


def repair_plan_is_safe(plan: Optional[dict]) -> bool:
    """Guardrail: refuse plans that could weaken security or lose data.

    Rules (PLAN section 3 flow 6): never delete environments, never touch
    secret values, never disable or weaken SSL/firewall/auth, never open new
    public endpoints. Plans are template-built, so this is a second pair of
    eyes — it must still refuse anything unexpected.
    """
    if not isinstance(plan, dict):
        return False
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return False
    for step in steps:
        if not isinstance(step, dict):
            return False
        tool = step.get("tool")
        if not isinstance(tool, str) or tool in FORBIDDEN_REPAIR_TOOLS:
            return False
        args = step.get("args")
        if not isinstance(args, dict):
            return False
        for key in args:
            if str(key).lower() in FORBIDDEN_REPAIR_ARGS:
                return False
        # never weaken security: removing SSL / forcing plaintext is forbidden
        if args.get("ssl") is False or args.get("disable_ssl") is True:
            return False
    return True


# --- classification ---------------------------------------------------------

#: platform error code -> (category, plain-language explanation, repairable)
_CLASSIFY = {
    "domain_unavailable": (
        "name_conflict",
        "The environment name you picked is already taken on this platform.",
        True),
    "manifest_corrupted": (
        "bad_package",
        "The application package could not be parsed — its manifest is "
        "corrupted (often invisible characters or broken indentation).",
        True),
    "operation_failed": (
        "operation_failed",
        "The platform accepted the request but the operation itself failed.",
        True),
    "env_not_found": (
        "env_not_found",
        "The environment does not exist (any more) — it may have been "
        "deleted or renamed.",
        False),
    "node_not_found": (
        "node_not_found",
        "That node does not exist in the environment — the topology may "
        "have changed.",
        False),
    "session_expired": (
        "session_expired",
        "Your platform session has expired — I cannot act until you "
        "re-open the dashboard and sign in again.",
        False),
    "auth_failed": (
        "auth_failed",
        "The platform rejected the credentials — check your account "
        "password.",
        False),
    "invalid_parameter": (
        "invalid_parameter",
        "The request carried a missing or invalid parameter.",
        False),
    "delete_in_progress": (
        "delete_in_progress",
        "The environment is currently being deleted — wait for it to "
        "finish.",
        False),
    "no_credentials": (
        "no_credentials",
        "No platform session is attached to this conversation.",
        False),
    "confirm_required": (
        "confirm_required",
        "The action was refused because it was not explicitly confirmed.",
        False),
    "password_required": (
        "password_required",
        "The action requires the account password.",
        False),
}

_DEFAULT_CLASS = (
    "platform_error",
    "The platform returned an unexpected error.",
    False)


async def gather_evidence(ctx: FlowContext, env_name: Optional[str]) -> List[dict]:
    """Read-only evidence around the failure: env state, stats, recent logs.

    Every read is best-effort and never raises — evidence gathering must not
    turn one failure into two.
    """
    evidence: List[dict] = []
    if not env_name:
        return evidence
    for name, args, label in (
            ("env_info", {"env_name": env_name}, "state"),
            ("env_stats", {"env_name": env_name}, "usage"),
            ("logs_read", {"env_name": env_name, "limit": 20}, "logs"),
            ("file_list", {"env_name": env_name, "path": "/"}, "files")):
        try:
            outcome = await ctx.tools.run(name, args)
        except Exception as exc:  # evidence must never crash diagnosis
            evidence.append({"label": label, "ok": False,
                             "summary": f"{name} unavailable: {exc}"})
            continue
        evidence.append({"label": label, "ok": outcome.ok,
                         "summary": outcome.summary})
    return evidence


def _sanitize_manifest(jps: str) -> str:
    """Strip the invisible characters that break manifest parsing."""
    cleaned = "".join(ch for ch in jps if ord(ch) < 128 and ch != "\t")
    return cleaned.strip()


def _build_repair_plan(error_code: str, tool: Optional[str],
                       args: Optional[dict],
                       summary: str = "") -> Optional[dict]:
    """Template repair plan for a repairable failure class (or None)."""
    args = dict(args or {})
    if args.get("env_name") and re.search(
            r"env_name.*(at most|match|character)|must be at most|"
            r"must match pattern", summary or ""):
        fixed = dict(args)
        fixed["env_name"] = slugify_env_name(str(args.get("env_name")))
        if fixed["env_name"] != args.get("env_name") and tool:
            return {
                "class": "fix_env_name",
                "recap": (f"Shorten the environment name to "
                          f"`{fixed['env_name']}` and run [{tool}] again"),
                "steps": [{"tool": tool, "args": fixed}],
                "verify": {"tool": "env_info",
                           "args": {"env_name": fixed["env_name"]}},
            }
        return None
    if error_code == "domain_unavailable" and tool == "env_create":
        new_args = dict(args)
        new_args.pop("env_name", None)   # filled by the apply step (asks)
        return {
            "class": "recreate_env",
            "recap": ("Create the environment under a different, available "
                      "name and continue with the same topology"),
            "needs": "env_name",
            "steps": [{"tool": "env_create", "args": new_args}],
            "verify": {"tool": "env_info", "args_key": "env_name"},
        }
    if error_code == "manifest_corrupted" and tool == "deploy_jps":
        jps = str(args.get("jps") or "")
        cleaned = _sanitize_manifest(jps)
        if cleaned and cleaned != jps:
            step_args = dict(args)
            step_args["jps"] = cleaned
            return {
                "class": "sanitize_manifest",
                "recap": ("Strip the invisible characters that broke the "
                          "manifest and install the package again"),
                "steps": [{"tool": "deploy_jps", "args": step_args}],
                "verify": {"tool": "env_info",
                           "args": {"env_name": args.get("env_name")}},
            }
        return None
    if error_code == "operation_failed" and tool in (
            "deploy_archive", "deploy_jps", "env_start", "node_scale",
            "db_add", "ssl_letsencrypt", "env_create"):
        return {
            "class": "retry_step",
            "recap": f"Retry [{tool}] — the failure may be transient",
            "steps": [{"tool": tool, "args": args}],
            "verify": {"tool": "env_info",
                       "args": {"env_name": args.get("env_name")}},
        }
    return None


async def diagnose(ctx: FlowContext, outcome: ToolOutcome, *,
                   env_name: Optional[str] = None,
                   step_args: Optional[dict] = None) -> Diagnosis:
    """Classify one failed tool outcome and propose a repair plan."""
    error_code = "platform_error"
    if isinstance(outcome.payload, dict):
        error = outcome.payload.get("error")
        if isinstance(error, dict):
            error_code = str(error.get("code") or error_code)
        elif isinstance(error, str):
            error_code = error
    category, explanation, repairable = _CLASSIFY.get(
        error_code, _DEFAULT_CLASS)

    summary = str(getattr(outcome, "summary", "") or "")
    name_issue = (
        error_code in ("invalid_parameter", "bad_request", "param_rejected")
        and bool(step_args and step_args.get("env_name"))
        and re.search(r"env_name.*(at most|match|character)|must be at most|"
                      r"must match pattern", summary))
    if name_issue:
        slug = slugify_env_name(str((step_args or {}).get("env_name")))
        category = "invalid_env_name"
        explanation = (
            "The environment name I used is not valid on this platform — "
            "names are lowercase letters, digits and dashes, up to 30 "
            f"characters. I can shorten it to `{slug}` and retry.")
        repairable = True

    if name_issue:
        # env_info/env_stats/logs would fail on the same bad name — one
        # honest line instead of three identical raw errors.
        evidence = [{"label": "name", "ok": False,
                     "summary": ("environment name is not a valid platform "
                                 "name")}]
    else:
        evidence = await gather_evidence(ctx, env_name)

    repair_plan = None
    if repairable:
        candidate = _build_repair_plan(error_code, outcome.name, step_args,
                                       summary=summary)
        if repair_plan_is_safe(candidate):
            repair_plan = candidate

    return Diagnosis(category=category, explanation=explanation,
                     evidence=evidence, repair_plan=repair_plan)
