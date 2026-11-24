"""Secret masking and human summaries for tool results.

Contract rules (docs/API-CONTRACT.md security notes):
- passwords/tokens never appear in ``tool_result`` summaries or logs;
- raw JSON payloads never reach the user — summaries only.

This module is the single place that turns tool payloads into the strings
that are streamed, logged into the audit trail, or fed back to the LLM.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "****"

#: argument names whose values are always masked
_SECRET_ARG_NAMES = {"password", "passwd", "secret", "token", "api_key",
                     "apikey", "access_token", "session", "key"}

#: environment-variable names that look like secrets (security review scan)
SECRET_VAR_RE = re.compile(
    r"(?i)(pass(word|wd)?|secret|token|api[-_]?key|credential|auth)")


def mask_value(value: Any) -> str:
    """A secret value is never shown — not even partially."""
    return MASK


def mask_args(name: str, args: dict) -> dict:
    """Mask secret-bearing arguments for the ``tool_call`` event."""
    masked: dict = {}
    for key, value in args.items():
        lowered = key.lower()
        if lowered in _SECRET_ARG_NAMES:
            masked[key] = MASK
        elif lowered in ("vars", "settings") and isinstance(value, dict):
            masked[key] = {k: (MASK if not isinstance(v, (dict, list))
                               else v) for k, v in value.items()}
        else:
            masked[key] = value
    return masked


def _vars_list(payload: Any) -> list:
    if isinstance(payload, dict):
        for key in ("vars", "envVars"):
            if isinstance(payload.get(key), dict):
                return sorted(payload[key].keys())
    return []


def summarize(name: str, args: dict, payload: Any, ok: bool) -> str:
    """One-line human summary of a tool result (safe for logs + LLM)."""
    if not ok:
        message = "platform error"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or message)
            elif isinstance(error, str):
                message = error
        return f"{name} failed: {message}"

    if not isinstance(payload, dict):
        return f"{name} completed"

    if name == "envs_list":
        count = payload.get("count", len(payload.get("environments", [])))
        return f"{count} environment(s) found"
    if name == "env_info":
        env = payload.get("env") or {}
        return (f"environment [{env.get('envName')}] is "
                f"{env.get('statusName', 'unknown')} "
                f"({len(env.get('nodes') or [])} node(s))")
    if name == "env_stats":
        nodes = payload.get("nodes") or []
        return f"usage read for {len(nodes)} node(s) of [{payload.get('env_name')}]"
    if name == "env_create":
        return (f"creation of environment [{args.get('env_name')}] accepted "
                f"(asynchronous)")
    if name in ("env_start", "env_stop", "env_sleep"):
        verb = {"env_start": "start", "env_stop": "stop",
                "env_sleep": "sleep"}[name]
        return f"{verb} requested for [{args.get('env_name')}]"
    if name == "env_delete":
        return f"environment [{args.get('env_name')}] deleted and verified gone"
    if name == "node_scale":
        return (f"node {args.get('node_id')} of [{args.get('env_name')}] set "
                f"to {args.get('fixed_cloudlets')} fixed + "
                f"{args.get('flexible_cloudlets')} flexible cloudlets")
    if name == "topology_change":
        return f"topology change accepted for [{args.get('env_name')}]"
    if name == "deploy_archive":
        return (f"deployed {args.get('url') or args.get('archive')} to "
                f"[{args.get('env_name')}] context "
                f"{args.get('context') or 'ROOT'}")
    if name == "deploy_jps":
        jps = str(args.get("jps") or "")
        shown = jps if jps.startswith(("http://", "https://")) else "<inline>"
        return f"installed {shown} into [{args.get('env_name')}]"
    if name == "db_add":
        return (f"database node group added to [{args.get('env_name')}] "
                f"(asynchronous)")
    if name == "logs_read":
        return f"{payload.get('count', 0)} log entry(ies) read"
    if name == "env_var_get":
        names = _vars_list(payload)
        return (f"{len(names)} variable(s): " + ", ".join(names)) if names \
            else "no variables set"
    if name == "env_var_set":
        raw = args.get("vars")
        if not isinstance(raw, dict):
            raw = {}
        names = sorted(raw.keys())
        return (f"{len(names)} variable(s) set (values masked): "
                + ", ".join(names))
    if name == "ssl_letsencrypt":
        return (f"Let's Encrypt certificate issued for "
                f"{args.get('ext_domain')}")
    if name == "marketplace_list":
        if "apps" in payload:
            return f"{len(payload.get('apps') or [])} app(s) in the catalog"
        return f"{len(payload.get('categories') or [])} catalog categories"
    if name == "templates_list":
        return f"{payload.get('count', 0)} stack template(s) available"
    if name == "quota_get":
        quotas = payload.get("quotas") or {}
        parts = [f"{k} {v.get('usage')}/{v.get('limit')}"
                 for k, v in quotas.items() if isinstance(v, dict)]
        return "quotas: " + ", ".join(parts) if parts else "quotas read"
    if name == "op_result":
        status = payload.get("status")
        if status == "done":
            return f"operation complete ({payload.get('op')})"
        return (f"operation still in progress ({payload.get('op')}); "
                f"last observed: {str(payload.get('observed'))[:80]}")
    return f"{name} completed"
