"""Guided flows: deploy_app, db_add, scale, troubleshoot, security_review.

Deterministic state machines the LLM starts via ``start_flow`` directives
(docs/PLAN.md section 3). Each step asks ONE clarifying question with options
(``ask_user``), nothing mutates without an approved ``confirm_action``, and
every failure goes through error -> diagnose -> auto-fix
(:mod:`agent.diagnose`) instead of dead-ending.

Flow contract (shared by tests):
- every mutating tool runs with ``confirmed=True`` ONLY after ``ctx.confirm``
  returned True for exactly that action;
- deploy ends with a verify step (``verify_http``: fetch the env URL) and a
  security review card (PLAN section 3 flow 6);
- raw tool JSON never reaches the user: narration + cards + summaries only.
"""

from __future__ import annotations

import json
import re
from typing import Optional, Tuple

from .context import FlowContext, match_option
from .diagnose import diagnose, repair_plan_is_safe
from .tools import ToolOutcome
from .tools_util import SECRET_VAR_RE, mask_value, slugify_env_name

from mcp.tools import TOOLS

#: platform env names: lowercase letter, then letters/digits/dashes (<=30)
ENV_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,29}$")

_CLOUDLET_RE = re.compile(r"\d+")

#: engine display -> nodeType used by the platform
_ENGINES = {
    "mysql": "mysql",
    "mariadb": "mariadb",
    "postgresql": "postgres",
    "postgres": "postgres",
    "mongodb": "mongodb",
    "redis": "redis",
}
_ENGINE_VERSIONS = {
    "mysql": "8.0", "mariadb": "10.11", "postgres": "16",
    "mongodb": "7.0", "redis": "7.2",
}
_ENGINE_PORTS = {
    "mysql": 3306, "mariadb": 3306, "postgres": 5432,
    "mongodb": 27017, "redis": 6379,
}

#: marketplace app name -> inline JPS manifest (no invented URLs)
def _marketplace_manifest(app_name: str) -> str:
    return json.dumps({"jpsType": "install", "name": app_name,
                       "version": "1.0"})


# --- small normalizers ------------------------------------------------------

def _normalize_source(answer: str) -> Optional[str]:
    mapped = match_option(answer, ["Marketplace", "Archive URL", "JPS package"])
    lowered = mapped.lower()
    if "marketplace" in lowered:
        return "marketplace"
    if "jps" in lowered:
        return "jps"
    if "archive" in lowered:
        return "archive"
    return None


def _normalize_db(answer: str) -> Optional[str]:
    """'No database' -> None; 'PostgreSQL 16' -> 'postgres:16'."""
    lowered = (answer or "").lower()
    if "no " in lowered or lowered.strip() in ("", "none", "no"):
        return None
    for key, node_type in _ENGINES.items():
        if key in lowered:
            version = None
            found = re.search(r"\d+(\.\d+)*", answer)
            if found:
                version = found.group(0)
            version = version or _ENGINE_VERSIONS.get(node_type, "")
            return f"{node_type}:{version}" if version else node_type
    return None


def _normalize_engine(answer: str) -> Optional[str]:
    mapped = match_option(answer, ["MySQL", "MariaDB", "PostgreSQL", "MongoDB", "Redis"])
    lowered = mapped.lower()
    for key, node_type in _ENGINES.items():
        if key in lowered:
            return node_type
    return None


def _parse_cloudlets(answer: str, default: int = 8) -> int:
    found = _CLOUDLET_RE.search(answer or "")
    if not found:
        return default
    value = int(found.group(0))
    return value if 1 <= value <= 64 else default


def _parse_ssl(answer) -> bool:
    if isinstance(answer, bool):
        return answer
    lowered = str(answer or "").strip().lower()
    if lowered.startswith("y") or lowered in ("true", "1", "on", "enable") \
            or "enable" in lowered or "yes" in lowered:
        return True
    return False


def _parse_version(answer: str, engine: str) -> str:
    found = re.search(r"\d+(\.\d+)*", answer or "")
    return found.group(0) if found else _ENGINE_VERSIONS.get(engine, "latest")


# --- error -> diagnose -> auto-fix ------------------------------------------

async def handle_failure(ctx: FlowContext, outcome: ToolOutcome, *,
                         step_args: Optional[dict] = None,
                         retry_tool: Optional[str] = None
                         ) -> Tuple[Optional[ToolOutcome], dict]:
    """Diagnose a failed step and offer a one-click auto-fix card.

    Returns ``(repaired_outcome_or_None, applied_args)``. When the repair
    re-runs the SAME tool, its outcome replaces the failed one and the flow
    may continue with ``applied_args`` (e.g. the new env name). Otherwise the
    caller should stop the flow: the repair emits its own cards.
    """
    env_name = (step_args or {}).get("env_name") or ctx.params.get("env_name")
    diagnosis = await diagnose(ctx, outcome, env_name=env_name,
                               step_args=step_args)

    body = f"**What happened:** {diagnosis.explanation}\n\n"
    body += f"**Failed step:** `{outcome.name}` — {outcome.summary}\n\n"
    if diagnosis.evidence:
        body += "**What I checked:**\n" + "".join(
            f"- {item['label']}: {item['summary']}\n"
            for item in diagnosis.evidence)
    actions = []
    plan_id = None
    if diagnosis.repair_plan is not None:
        plan_id = ctx.conversation.register_repair_plan(diagnosis.repair_plan)
        actions.append({"label": "Fix it automatically",
                        "url": "", "action": "auto_fix",
                        "payload": {"repair_plan_id": plan_id}})
    retry_name = retry_tool or outcome.name
    if retry_name in TOOLS:
        actions.append({"label": "Retry the step", "url": "",
                        "action": "retry",
                        "payload": {"tool": retry_name}})
    actions.append({"label": "Open the dashboard",
                    "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                    "payload": {}})

    await ctx.card("error", "Something went wrong", body, actions)
    if not actions:
        return None, {}

    choice = await ctx.wait_card_action(actions)
    if choice is None:
        await ctx.say("Okay — I have left everything as it is.")
        return None, {}

    if choice.get("action") == "auto_fix":
        plan = ctx.conversation.get_repair_plan(
            (choice.get("payload") or {}).get("repair_plan_id"))
        return await apply_repair(ctx, plan), (
            (plan or {}).get("steps", [{}])[0].get("args") or {})

    if choice.get("action") == "retry":
        tool = (choice.get("payload") or {}).get("tool") or outcome.name
        args = dict(step_args or {})
        spec_args = args or {}
        approved = True
        if tool in ("env_create", "deploy_archive", "deploy_jps", "env_start",
                    "node_scale", "db_add", "ssl_letsencrypt"):
            approved = await ctx.confirm(
                f"Retry [{tool}] on the same target?",
                actions=[tool])
        if not approved:
            await ctx.say("Retry cancelled.")
            return None, {}
        fresh = await ctx.run_tool(tool, spec_args, confirmed=approved)
        return fresh, spec_args

    return None, {}


async def apply_repair(ctx: FlowContext, plan: Optional[dict]
                       ) -> Optional[ToolOutcome]:
    """Guarded repair: guardrails -> recap -> confirm -> apply -> verify."""
    if plan is None:
        await ctx.say("I could not find a repair plan for this.")
        return None
    if not repair_plan_is_safe(plan):
        await ctx.say("I will not run this repair: it could weaken security "
                      "or cause data loss. Please do it explicitly from the "
                      "dashboard.")
        return None

    needs = plan.get("needs")
    applied_args = {}
    steps = []
    for step in plan.get("steps", []):
        args = dict(step.get("args") or {})
        if needs == "env_name":
            name = await ctx.ask(
                "What should the new environment be called? "
                "(lowercase letters, digits, dashes)",
                allow_free=True,
                validate=lambda a: (None if ENV_NAME_RE.match(a.strip())
                                    else "Please use lowercase letters, "
                                         "digits and dashes (must start with "
                                         "a letter)."))
            args["env_name"] = name.strip()
            applied_args["env_name"] = args["env_name"]
        steps.append({"tool": step["tool"], "args": args})

    recap = plan.get("recap", "Apply the repair")
    listing = "; ".join(f"[{s['tool']}]" for s in steps)
    approved = await ctx.confirm(f"Repair plan: {recap}. Steps: {listing}.",
                                 actions=[s["tool"] for s in steps])
    if not approved:
        await ctx.say("Repair cancelled — nothing was changed.")
        return None

    last: Optional[ToolOutcome] = None
    for step in steps:
        last = await ctx.run_tool(step["tool"], step["args"], confirmed=True,
                                  progress=f"Repairing: {step['tool']}")
        if not last.ok:
            await ctx.card(
                "error", "The repair did not work",
                f"I tried to {recap} but **[{step['tool']}]** still failed: "
                f"{last.summary}\n\nWhat to try next: check the environment "
                f"in the dashboard, or tell me to try again.",
                [{"label": "Open the dashboard",
                  "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                  "payload": {}}])
            return last

    verify = plan.get("verify") or {}
    verified = False
    verify_summary = ""
    if verify.get("tool") == "env_info":
        v_args = dict(verify.get("args") or {})
        if "env_name" not in v_args:
            v_args["env_name"] = applied_args.get("env_name") or \
                (steps[-1]["args"].get("env_name") if steps else None)
        if v_args.get("env_name"):
            check = await ctx.run_tool("env_info", v_args)
            verified = check.ok
            verify_summary = check.summary
    if verified:
        await ctx.card(
            "success", "Fixed and verified",
            f"{recap} — done and verified ({verify_summary}).", [])
    else:
        await ctx.card(
            "info", "Repair applied, verification pending",
            f"{recap} — the repair was applied but I could not fully verify "
            f"it yet. Please check the environment in the dashboard.", [])
    return last


# --- security & compliance (PLAN section 3 flow 6) --------------------------

async def security_review(ctx: FlowContext, env_name: str, *,
                          wait_for_fix: bool = True) -> None:
    """In-process security review: secrets scan, exposure, SSL status.

    Findings render as a ``kind:security`` card with one-click fixes; fix
    actions run through the same guarded repair path (recap -> confirm ->
    apply -> verify). Auto-fix may never weaken security.
    """
    findings = []      # (severity, text, repair_plan or None)

    info = await ctx.run_tool("env_info", {"env_name": env_name})
    if not info.ok:
        await ctx.say("I could not run the security review on this "
                      "environment.")
        return
    env = info.payload.get("env") or {}
    domain = str(env.get("domain") or "")
    ssl = env.get("ssl")

    # 1. secrets scan (values never shown — masked at the source)
    vars_out = await ctx.run_tool("env_var_get", {"env_name": env_name})
    variables = {}
    if vars_out.ok:
        variables = vars_out.payload.get("vars") or {}
        if isinstance(variables, dict) and set(variables) == {"vars"} \
                and isinstance(variables.get("vars"), dict):
            variables = variables["vars"]
    secret_names = sorted(k for k in variables
                          if isinstance(k, str) and SECRET_VAR_RE.search(k))
    for name in secret_names:
        findings.append((
            "high",
            f"Environment variable `{name}` looks like a secret "
            f"(value: `{mask_value(None)}`). Make sure it is rotated "
            f"regularly and never committed to source code.", None))

    # 2. SSL status
    if not ssl:
        plan = None
        if domain:
            plan = {
                "class": "enable_ssl",
                "recap": f"Enable a free Let's Encrypt SSL certificate for "
                         f"{domain}",
                "steps": [{"tool": "ssl_letsencrypt",
                           "args": {"env_name": env_name,
                                    "ext_domain": domain}}],
                "verify": {"tool": "env_info", "args": {"env_name": env_name}},
            }
        findings.append((
            "high",
            f"**No SSL certificate** on `{domain or env_name}` — traffic is "
            f"not encrypted. A free Let's Encrypt certificate can fix this.",
            plan if repair_plan_is_safe(plan) else None))

    # 3. exposure check (database nodes should not be public)
    db_nodes = [n for n in (env.get("nodes") or [])
                if n.get("nodeGroup") == "db"]
    if db_nodes:
        findings.append((
            "medium",
            "A **database node** is attached. By default it is reachable "
            "only from inside the environment — keep it that way and check "
            "the firewall before opening database ports publicly.", None))

    body = "### Security review\n\n"
    if not findings:
        body += "No issues found. Secrets are masked, transport is encrypted." \
            if ssl else "No issues found."
        await ctx.card("security", f"Security review — {env_name}", body, [])
        return

    fix_actions = []
    for severity, text, plan in findings:
        pill = {"high": "HIGH", "medium": "MEDIUM"}.get(severity, "LOW")
        body += f"- **[{pill}]** {text}\n"
        if plan is not None:
            plan_id = ctx.conversation.register_repair_plan(plan)
            fix_actions.append({
                "label": f"Fix: {plan['recap']}", "url": "",
                "action": "auto_fix",
                "payload": {"repair_plan_id": plan_id}})

    await ctx.card("security", f"Security review — {env_name}", body,
                   fix_actions)
    if fix_actions and wait_for_fix:
        choice = await ctx.wait_card_action(fix_actions)
        if choice is not None and choice.get("action") == "auto_fix":
            plan = ctx.conversation.get_repair_plan(
                (choice.get("payload") or {}).get("repair_plan_id"))
            await apply_repair(ctx, plan)
        elif choice is not None:
            await ctx.say("Okay — the finding stays open; you can fix it "
                          "later from the dashboard.")


# --- flow 1: deploy app -----------------------------------------------------

async def deploy_app(ctx: FlowContext) -> None:
    p = ctx.params

    # 1. source -----------------------------------------------------------
    source = _normalize_source(str(p.get("source") or "")) if p.get("source") \
        else None
    if source is None:
        answer = await ctx.ask(
            "Where is your application coming from?",
            ["Marketplace", "Archive URL", "JPS package"])
        source = _normalize_source(answer) or "archive"

    app_desc = ""
    jps_manifest = None
    archive_url = None
    if source == "marketplace":
        catalog = await ctx.run_tool("marketplace_list", {"category": 1})
        names = [str(a.get("name")) for a in
                 ((catalog.payload or {}).get("apps") or [])]
        if p.get("app"):
            app_desc = str(p["app"])
        else:
            app_desc = await ctx.ask(
                "Which application would you like to install?",
                names or ["WordPress"])
            if names:
                app_desc = match_option(app_desc, names)
        jps_manifest = _marketplace_manifest(app_desc)
    elif source == "archive":
        archive_url = str(p.get("url") or "") or await ctx.ask(
            "What is the URL of your application archive (.tar/.zip)?",
            allow_free=True,
            validate=lambda a: (None if a.strip().startswith(
                ("http://", "https://"))
                else "Please give an http(s) URL to the archive."))
        archive_url = archive_url.strip()
        app_desc = archive_url
    else:  # jps
        jps_manifest = str(p.get("jps") or "") or await ctx.ask(
            "Paste the JPS package (a manifest URL or the manifest itself):",
            allow_free=True)
        jps_manifest = jps_manifest.strip()
        app_desc = jps_manifest if jps_manifest.startswith(
            ("http://", "https://")) else "an inline JPS package"

    # 2. target environment ------------------------------------------------
    creating = False
    env_name = slugify_env_name(str(p.get("env_name") or "")) if p.get("env_name") else ""
    if env_name:
        probe = await ctx.run_tool("env_info", {"env_name": env_name})
        creating = not probe.ok
    else:
        listed = await ctx.run_tool("envs_list", {})
        names = [str(e.get("envName")) for e in
                 ((listed.payload or {}).get("environments") or [])]
        options = names + ["Create a new environment"]
        choice = await ctx.ask("Which environment should I deploy into?",
                               options)
        if choice == "Create a new environment":
            creating = True
        else:
            env_name = match_option(choice, names)
    if creating:
        existing = set()
        listed = await ctx.run_tool("envs_list", {})
        if listed.ok:
            existing = {str(e.get("envName")) for e in
                        (listed.payload.get("environments") or [])}

        def _check_name(answer: str) -> Optional[str]:
            name = answer.strip()
            if not ENV_NAME_RE.match(name):
                return ("Please use lowercase letters, digits and dashes "
                        "(must start with a letter, max 30 characters).")
            if name in existing:
                return f"[{name}] already exists — pick a different name."
            return None

        env_name = (env_name or await ctx.ask(
            "What should the new environment be called?",
            allow_free=True, validate=_check_name)).strip()

    # 3. topology ----------------------------------------------------------
    stack = str(p.get("stack") or "").strip() or await ctx.ask(
        "Which application stack?",
        ["php:8.3", "nodejs:20", "java:21", "python:3.12"])
    db = None
    if p.get("db") is not None:
        db = _normalize_db(str(p["db"])) if p["db"] else None
    else:
        db = _normalize_db(await ctx.ask(
            "Would you like a database with it?",
            ["No database", "MySQL 8.0", "MariaDB 10.11", "PostgreSQL 16",
             "Redis 7.2"]))

    # 4. resources ---------------------------------------------------------
    cloudlets = _parse_cloudlets(str(p["cloudlets"])) if p.get("cloudlets") \
        else _parse_cloudlets(await ctx.ask(
            "How much power should it get?",
            ["Dev - 4 cloudlets", "Prod - 12 cloudlets"]), default=8)

    # 5. domain / SSL ------------------------------------------------------
    ssl_param = p.get("ssl")
    ssl = _parse_ssl(ssl_param) if ssl_param is not None else \
        _parse_ssl(await ctx.ask(
            "Enable a free Let's Encrypt SSL certificate?",
            ["Yes, enable SSL", "No, skip SSL"]))

    # RECAP -> confirm -----------------------------------------------------
    db_part = f" + {db}" if db else ""
    recap = (f"Create environment **{env_name}** with `{stack}`{db_part}, "
             f"{cloudlets} cloudlets per node, deploy {app_desc}"
             + (", free SSL on" if ssl else ", SSL off") + ".")
    approved = await ctx.confirm(recap, actions=["create environment",
                                                 "deploy application"])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    # execute --------------------------------------------------------------
    if creating:
        create_args = {"env_name": env_name, "stack": stack,
                       "cloudlets": cloudlets}
        if db:
            create_args["db"] = db
        out = await ctx.run_tool("env_create", create_args, confirmed=True,
                                 progress="Creating the environment")
        if not out.ok:
            out, applied = await handle_failure(ctx, out, step_args=create_args,
                                                retry_tool="env_create")
            if out is None or not out.ok:
                return
            env_name = applied.get("env_name", env_name)
        out = await ctx.run_tool("op_result", {
            "env_name": env_name, "op": "status", "target": ["running"],
            "timeout": 60}, progress="Waiting for the environment to start")
        if not out.ok or out.payload.get("status") != "done":
            await ctx.say("The environment is still starting up. I will "
                          "stop here — ask me to continue in a moment.")
            return

    # deploy the application ----------------------------------------------
    if source == "archive":
        step_args = {"env_name": env_name, "url": archive_url,
                     "context": "ROOT"}
        out = await ctx.run_tool("deploy_archive", step_args, confirmed=True,
                                 progress="Deploying your application")
    else:
        step_args = {"env_name": env_name, "jps": jps_manifest}
        out = await ctx.run_tool("deploy_jps", step_args, confirmed=True,
                                 progress="Installing your application")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args)
        if out is None or not out.ok:
            return

    # SSL ------------------------------------------------------------------
    info = await ctx.run_tool("env_info", {"env_name": env_name})
    domain = str((info.payload.get("env") or {}).get("domain") or "")
    if ssl and domain:
        out = await ctx.run_tool("ssl_letsencrypt", {
            "env_name": env_name, "ext_domain": domain}, confirmed=True,
            progress="Issuing a free SSL certificate")
        if not out.ok:
            await ctx.say("The SSL certificate could not be issued right "
                          "now — the app itself deployed fine.")

    # verify (curl the env URL) -------------------------------------------
    scheme = "https" if ssl else "http"
    app_url = f"{scheme}://{domain}/" if domain else ""
    if app_url:
        verified = await ctx.verify_url(app_url)
        if not verified.ok:
            await handle_failure(ctx, verified, step_args={"env_name": env_name})
            return

    # success card ---------------------------------------------------------
    body = (f"- Environment: `{env_name}`\n"
            f"- URL: `{app_url}`\n"
            f"- Stack: `{stack}`" + (f" + `{db}`" if db else "") + "\n"
            f"- Resources: {cloudlets} cloudlets per node\n")
    await ctx.card(
        "success", "Deployment complete", body,
        [{"label": "Open app", "url": app_url, "action": "open_url",
          "payload": {}},
         {"label": "Open dashboard",
          "url": f"{ctx.tools.auth.host}/", "action": "open_url",
          "payload": {}}])

    # security review is the last step of the deploy wizard ---------------
    await security_review(ctx, env_name, wait_for_fix=True)


# --- flow 2: add database ---------------------------------------------------

async def db_add(ctx: FlowContext) -> None:
    p = ctx.params

    env_name = slugify_env_name(str(p.get("env_name") or "")) if p.get("env_name") else ""
    if env_name:
        probe = await ctx.run_tool("env_info", {"env_name": env_name})
        if not probe.ok:
            env_name = ""   # unknown target — fall through to the picker
    if not env_name:
        listed = await ctx.run_tool("envs_list", {})
        names = [str(e.get("envName")) for e in
                 ((listed.payload or {}).get("environments") or [])]
        env_name = match_option(await ctx.ask(
            "Which environment should the database attach to?", names),
            names)

    engine = _normalize_engine(str(p.get("engine") or "")) if p.get("engine") \
        else _normalize_engine(await ctx.ask(
            "Which database engine?",
            ["MySQL", "MariaDB", "PostgreSQL", "MongoDB", "Redis"]))
    engine = engine or "mysql"

    version = str(p.get("version") or "").strip() or _parse_version(
        await ctx.ask(f"Which version of {engine}?",
                      ["Latest stable", _ENGINE_VERSIONS.get(engine, "8.0")]),
        engine)

    cloudlets = _parse_cloudlets(str(p["cloudlets"])) if p.get("cloudlets") \
        else _parse_cloudlets(await ctx.ask(
            "How much power should the database get?",
            ["Dev - 4 cloudlets", "Prod - 12 cloudlets"]), default=8)

    if p.get("credentials") is None:
        choice = await ctx.ask(
            "How should the database credentials be handled?",
            ["Generate automatically (recommended)",
             "I'll set them in the dashboard"])
        await ctx.say("The platform generates the credentials — you can "
                      "see them in the dashboard; I never show passwords "
                      "here." if "generate" in choice.lower()
                      else "You can set the credentials in the dashboard "
                           "after the database is up.")

    recap = (f"Add a **{engine} {version}** database node to `{env_name}` "
             f"({cloudlets} cloudlets).")
    approved = await ctx.confirm(recap, actions=["add database node"])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    step_args = {"env_name": env_name, "engine": engine, "version": version,
                 "cloudlets": cloudlets}
    out = await ctx.run_tool("db_add", step_args, confirmed=True,
                             progress="Adding the database node")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args,
                                            retry_tool="db_add")
        if out is None or not out.ok:
            return

    out = await ctx.run_tool("op_result", {
        "env_name": env_name, "op": "nodes_ready", "timeout": 60},
        progress="Waiting for the database node")
    if not out.ok or out.payload.get("status") != "done":
        await ctx.say("The database node is still being provisioned — "
                      "ask me to check in a moment.")
        return

    info = await ctx.run_tool("env_info", {"env_name": env_name})
    domain = str((info.payload.get("env") or {}).get("domain") or "")
    port = _ENGINE_PORTS.get(engine, 0)
    conn = f"{engine}://{domain}:{port}" if domain else f"{engine}://<host>:{port}"
    body = (f"- Database: `{engine} {version}` on `{env_name}`\n"
            f"- Connection string: `{conn}`\n"
            f"- Credentials: available in the dashboard (never shown here)\n\n"
            f"_Security note: the database is reachable only from inside the "
            f"environment by default — check the firewall before exposing "
            f"ports publicly._")
    await ctx.card("success", "Database added", body,
                   [{"label": "Open dashboard",
                     "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                     "payload": {}}])


# --- flow 3: scale ----------------------------------------------------------

async def scale(ctx: FlowContext) -> None:
    p = ctx.params

    env_name = slugify_env_name(str(p.get("env_name") or "")) if p.get("env_name") else ""
    if not env_name:
        listed = await ctx.run_tool("envs_list", {})
        names = [str(e.get("envName")) for e in
                 ((listed.payload or {}).get("environments") or [])]
        env_name = match_option(await ctx.ask(
            "Which environment should I scale?", names), names)

    stats = await ctx.run_tool("env_stats", {"env_name": env_name},
                               progress="Reading current usage")
    nodes = (stats.payload or {}).get("nodes") or []
    hottest = None
    for node in nodes:
        load = float(node.get("cpu") or node.get("ram") or 0)
        if hottest is None or load > float(hottest.get("cpu") or
                                           hottest.get("ram") or 0):
            hottest = node
    if hottest is None:
        await ctx.say("I could not read the usage for this environment.")
        return

    node_id = hottest.get("id") or hottest.get("nodeId")
    current = int(hottest.get("fixedCloudlets") or 0) + \
        int(hottest.get("flexibleCloudlets") or 0)
    size_note = f" with {current} cloudlets" if current else ""
    await ctx.say(f"Node **{hottest.get('displayName') or node_id}** is "
                  f"using CPU {hottest.get('cpu')}% / RAM "
                  f"{hottest.get('ram')}%{size_note}.")

    choice = await ctx.ask(
        f"How should I scale node {node_id}?",
        ["Scale to 12 cloudlets (Prod)", "Scale to 24 cloudlets (High load)",
         "Keep it as is"])
    target = _parse_cloudlets(choice, default=0)
    if not target or "keep" in choice.lower():
        await ctx.say("Understood — I will leave the size as it is.")
        return

    approved = await ctx.confirm(
        f"Scale node {node_id} of `{env_name}` to {target} cloudlets "
        f"(1 fixed + {target - 1} flexible).",
        actions=["scale node"])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    step_args = {"env_name": env_name, "node_id": node_id,
                 "fixed_cloudlets": 1, "flexible_cloudlets": target - 1}
    out = await ctx.run_tool("node_scale", step_args, confirmed=True,
                             progress="Applying the new size")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args,
                                            retry_tool="node_scale")
        if out is None or not out.ok:
            return

    after = await ctx.run_tool("env_stats", {"env_name": env_name},
                               progress="Re-reading usage")
    await ctx.card(
        "success", "Scaling applied",
        f"Node {node_id} of `{env_name}` now runs with {target} cloudlets.\n"
        f"- After: {(after.summary if after.ok else 'usage not readable yet')}",
        [{"label": "Open dashboard", "url": f"{ctx.tools.auth.host}/",
          "action": "open_url", "payload": {}}])


# --- flow 4: troubleshoot ---------------------------------------------------

async def troubleshoot(ctx: FlowContext) -> None:
    p = ctx.params

    env_name = slugify_env_name(str(p.get("env_name") or "")) if p.get("env_name") else ""
    if not env_name:
        listed = await ctx.run_tool("envs_list", {})
        names = [str(e.get("envName")) for e in
                 ((listed.payload or {}).get("environments") or [])]
        env_name = match_option(await ctx.ask(
            "Which environment is misbehaving?", names), names)

    info = await ctx.run_tool("env_info", {"env_name": env_name},
                              progress="Reading environment state")
    stats = await ctx.run_tool("env_stats", {"env_name": env_name},
                               progress="Reading usage")
    logs = await ctx.run_tool("logs_read", {"env_name": env_name, "limit": 20},
                              progress="Reading recent logs")

    env = (info.payload or {}).get("env") or {}
    status = str(env.get("statusName") or "")
    plan = None
    explanation = ""

    if status in ("stopped", "sleeping"):
        explanation = (f"The environment is **{status}** — nothing can run "
                       f"until it is started.")
        plan = ("Start the environment again",
                {"tool": "env_start", "args": {"env_name": env_name}})
    else:
        hot = [n for n in ((stats.payload or {}).get("nodes") or [])
               if float(n.get("cpu") or 0) > 85 or float(n.get("ram") or 0)
               > 85]
        log_text = " ".join(str(entry) for entry in
                            ((logs.payload or {}).get("logs") or [])).lower()
        if hot:
            node = hot[0]
            explanation = (f"Node **{node.get('displayName')}** is overloaded "
                           f"(CPU {node.get('cpu')}%, RAM {node.get('ram')}%).")
            target = 12
            plan = ("Scale the hottest node up to 12 cloudlets",
                    {"tool": "node_scale",
                     "args": {"env_name": env_name, "node_id": node.get("id"),
                              "fixed_cloudlets": 1,
                              "flexible_cloudlets": target - 1}})
        elif "error" in log_text or "fail" in log_text:
            explanation = ("The recent logs contain errors — the last deploy "
                           "or a background job is failing.")
            plan = ("Restart the application environment",
                    {"tool": "env_start", "args": {"env_name": env_name}})
        else:
            explanation = ("Everything looks healthy: the environment is "
                           f"{status}, resources are fine and the recent "
                           "logs are clean.")
            await ctx.card("info", "Everything looks healthy", explanation,
                           [{"label": "Open dashboard",
                             "url": f"{ctx.tools.auth.host}/",
                             "action": "open_url", "payload": {}}])
            return

    await ctx.say(explanation)
    approved = await ctx.confirm(
        f"Proposed fix: {plan[0]}.", actions=[plan[1]["tool"]])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    out = await ctx.run_tool(plan[1]["tool"], plan[1]["args"], confirmed=True,
                             progress=f"Applying: {plan[0]}")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=plan[1]["args"],
                                            retry_tool=plan[1]["tool"])
        if out is None or not out.ok:
            return

    after = await ctx.run_tool("env_info", {"env_name": env_name},
                               progress="Re-checking the environment")
    now_summary = after.summary if after.ok else "not readable yet"
    await ctx.card(
        "success", "Fix applied",
        f"{plan[0]} — done.\nNow: {now_summary}.",
        [{"label": "Open dashboard", "url": f"{ctx.tools.auth.host}/",
          "action": "open_url", "payload": {}}])


# --- dispatcher -------------------------------------------------------------

FLOWS = {
    "deploy_app": deploy_app,
    "db_add": db_add,
    "scale": scale,
    "troubleshoot": troubleshoot,
    "security_review": None,   # special-cased below (needs env_name)
}


async def dispatch(ctx: FlowContext, flow: str, params: Optional[dict] = None
                   ) -> bool:
    """Run one guided flow by name. Returns False when the flow is unknown."""
    ctx.params.update(params or {})
    if flow == "security_review":
        env_name = slugify_env_name(str(ctx.params.get("env_name") or "")) if ctx.params.get("env_name") else ""
        if not env_name:
            listed = await ctx.run_tool("envs_list", {})
            names = [str(e.get("envName")) for e in
                     ((listed.payload or {}).get("environments") or [])]
            env_name = match_option(await ctx.ask(
                "Which environment should I review?", names), names)
        await security_review(ctx, env_name, wait_for_fix=True)
        return True
    fn = FLOWS.get(flow)
    if fn is None:
        return False
    await fn(ctx)
    return True
