"""Guided flows: deploy_app, db_add, scale, troubleshoot, security_review
plus the coverage workflows (T14): domain_setup / domain_unbind, clone_env,
autoscale_setup / trigger_manage, schedule_task, share_env, usage_report,
open_port / close_port, firewall_manage, restart.

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
from mcp.deploy_templates import (language_display, language_tier,
                                  looks_like_git_url, normalize_git_url)

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

_URL_IN_TEXT = re.compile(r"(?:https?://|git@)\S+")


def _extract_source_url(text: str) -> Optional[str]:
    """'deploy my app from https://github.com/o/r' -> the URL."""
    match = _URL_IN_TEXT.search(text or "")
    return match.group(0).rstrip(").,;]") if match else None


def _classify_source(answer: str) -> Optional[str]:
    """Answer text -> source kind: git | archive | marketplace | jps.

    Accepts plain URLs (git or archive), SSH-style git URLs and URLs buried
    in free text ('deploy my Rust app from <git url>').
    """
    value = (answer or "").strip()
    if not value:
        return None
    candidate = _extract_source_url(value)
    if candidate:
        return ("git" if looks_like_git_url(candidate) else "archive")
    lowered = value.lower()
    if re.search(r"\bgit(?:hub|lab)?\b", lowered) or "repository" in lowered \
            or "repo" in lowered:
        return "git"
    return _normalize_source(value)


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


def _map_stack_answer(answer: str) -> Tuple[int, Optional[str], str]:
    """Stack/image answer -> (tier, runtime_image, stack) via the tiered
    deploy table. Anything unrecognized is treated as a user-supplied
    registry image (tier 3) - the wizard must always reach a deploy plan.
    """
    text = (answer or "").strip()
    if "vps" in text.lower() and "node" in text.lower():
        return 4, "vps", "vps"
    if re.match(r"^(nodejs|python|java|php|ruby|golang|go|dotnet)(:|$)",
                text):
        return 1, None, text
    image = text if (":" in text or "/" in text) else f"{text}:latest"
    return 3, image, f"docker:{image}"


async def _ask_runtime_image(ctx: FlowContext
                             ) -> Tuple[int, Optional[str], str]:
    """Universal-start guarantee: an unknown runtime never errors out - we
    ask which runtime image to use (free text allowed: the user's own
    registry image) with the Tier 4 VPS bootstrap as the fallback chip."""
    answer = await ctx.ask(
        "Which runtime image should I use?",
        ["Deploy on a VPS node (bootstrap script)", "nodejs:20",
         "python:3.12", "php:8.3", "java:21"])
    return _map_stack_answer(answer)


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


#: friendly stack choices for the create-environment wizard
_STACK_OPTIONS = ["PHP 8.3", "Node.js 20", "Java 21", "Python 3.12"]

_STACK_TOKENS = (("php", "php:8.3"), ("node", "nodejs:20"),
                 ("java", "java:21"), ("python", "python:3.12"))


def _normalize_stack(answer: str) -> str:
    lowered = (answer or "").strip().lower()
    for key, token in _STACK_TOKENS:
        if key in lowered:
            return token
    cleaned = re.sub(r"[^a-z0-9.:+_-]", "", lowered.replace(" ", ""))
    return cleaned or "php:8.3"


async def _env_known(ctx: FlowContext, env_name: str) -> bool:
    """Existence check WITHOUT scary failure rows.

    Probing env_info for a missing environment renders as a red "failed"
    tool row and reads like a crash to users — but "does it exist yet?" is
    normal wizard control flow. List the environments and look the name up
    instead: one quiet success row, no error."""
    listed = await ctx.run_tool("envs_list", {})
    names = [str(e.get("envName")) for e in
             ((listed.payload or {}).get("environments") or [])]
    return env_name in names


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
    """Deploy ANY application (deploy-anything wizard, T12).

    Source: git URL / archive URL / marketplace / JPS. Git and archive
    sources run ``project_inspect`` so the stack step can propose the
    DETECTED stack as its primary chip; the deploy step routes through the
    tiered deploy table (tier 1 native stack + VCS/archive deploy, tier 2/3
    runtime-image deploy via ``deploy_any``, tier 4 VPS bootstrap) and the
    verify chain is op_result(nodes_ready) -> curl -> app_healthcheck ->
    success card, or error -> diagnose -> auto-fix. The wizard ALWAYS
    reaches a deployable answer - unknown languages ask for a runtime image.
    """
    p = ctx.params

    # 1. source -----------------------------------------------------------
    source = _classify_source(str(p.get("source") or "")) if p.get("source") \
        else None
    if source is None:
        answer = await ctx.ask(
            "Where is your application coming from?",
            ["Git repository", "Marketplace", "Archive URL", "JPS package"])
        source = _classify_source(answer) or "archive"

    app_desc = ""
    jps_manifest = None
    archive_url = None
    git_url = None
    detection = None
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
    elif source == "jps":
        jps_manifest = str(p.get("jps") or "") or await ctx.ask(
            "Paste the JPS package (a manifest URL or the manifest itself):",
            allow_free=True)
        jps_manifest = jps_manifest.strip()
        app_desc = jps_manifest if jps_manifest.startswith(
            ("http://", "https://")) else "an inline JPS package"
    else:
        # git repository or source archive: find the URL (params, free text
        # like 'deploy my app from <url>', or one guiding question).
        raw_url = ""
        if p.get("source"):
            raw_url = _extract_source_url(str(p["source"])) or \
                (str(p["source"]).strip() if str(p["source"]).strip().startswith(
                    ("http://", "https://", "git@")) else "")
        raw_url = raw_url or str(p.get("url") or "").strip()
        if not raw_url:
            if source == "git":
                question = "What is the Git repository URL?"
                valid = ("http://", "https://", "git@")
            else:
                question = ("What is the URL of your application archive "
                            "(.tar/.zip)?")
                valid = ("http://", "https://")
            raw_url = (await ctx.ask(
                question, allow_free=True,
                validate=lambda a: (None if a.strip().startswith(valid)
                                    else "Please give an http(s) URL."))
                       ).strip()
        raw_url = normalize_git_url(_extract_source_url(raw_url) or raw_url)
        if source == "git":
            git_url = raw_url
        else:
            archive_url = raw_url
        app_desc = raw_url
        # project_inspect detects language/version/evidence -> the stack
        # step proposes it as the primary chip (skipped when the caller
        # already pinned the stack).
        if not str(p.get("stack") or "").strip():
            insp = await ctx.run_tool("project_inspect",
                                      {"source_url": raw_url})
            if insp.ok:
                detection = insp.payload

    # 2. target environment ------------------------------------------------
    creating = False
    env_name = slugify_env_name(str(p.get("env_name") or "")) if p.get("env_name") else ""
    if env_name:
        creating = not await _env_known(ctx, env_name)
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
                return f"[{name}] already exists -- pick a different name."
            return None

        env_name = (env_name or await ctx.ask(
            "What should the new environment be called?",
            allow_free=True, validate=_check_name)).strip()

    # 3. topology: the DETECTED stack is the primary chip ------------------
    stack = str(p.get("stack") or "").strip()
    runtime_image = None
    deploy_port = None
    tier = 1
    detected = (detection or {}).get("detected") or []
    primary = None
    if detected:
        first = detected[0]
        version = f" {first.get('version')}" if first.get("version") else ""
        primary = (f"{language_display(str(first.get('type') or ''))}"
                   f"{version} "
                   f"(detected from {first.get('evidence') or 'its manifest'})")
    manual = ["nodejs:20", "python:3.12", "php:8.3", "java:21"]
    if not stack:
        if primary is not None:
            answer = await ctx.ask("Which application stack?",
                                   [primary] + manual)
        elif source in ("git", "archive"):
            answer = await ctx.ask(
                "Which runtime image should I use?",
                ["Deploy on a VPS node (bootstrap script)"] + manual)
        else:
            answer = await ctx.ask("Which application stack?", manual)
        stack = answer
    # route the answer through the tiered deploy table
    if source in ("git", "archive"):
        if primary is not None and stack == primary:
            kind = str(detected[0].get("type") or "")
            deploy_port = (detection or {}).get("suggested_port")
            runtime_image = (detection or {}).get("suggested_runtime_image")
            if language_tier(kind) == 1:
                tier = 1
                stack = (detection or {}).get("suggested_stack") or stack
            elif runtime_image:
                tier = 2
                stack = f"docker:{runtime_image}"
            else:
                tier, runtime_image, stack = await _ask_runtime_image(ctx)
        else:
            tier, runtime_image, stack = _map_stack_answer(stack)

    # 4. resources ---------------------------------------------------------
    db = None
    if p.get("db") is not None:
        db = _normalize_db(str(p["db"])) if p["db"] else None
    else:
        db = _normalize_db(await ctx.ask(
            "Would you like a database with it?",
            ["No database", "MySQL 8.0", "MariaDB 10.11", "PostgreSQL 16",
             "Redis 7.2"]))

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
        await ctx.say("Cancelled -- I changed nothing.")
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
                          "stop here -- ask me to continue in a moment.")
            return

    # deploy the application (tiered routes) -------------------------------
    if source == "git" and tier == 1:
        step_args = {"env_name": env_name, "git_url": git_url,
                     "context": "ROOT"}
        branch = str(p.get("branch") or "").strip() or \
            (detection or {}).get("default_branch")
        if branch:
            step_args["branch"] = branch
        out = await ctx.run_tool("deploy_vcs", step_args, confirmed=True,
                                 progress="Deploying your application")
    elif source in ("git", "archive") and tier >= 2:
        step_args = {"env_name": env_name,
                     "source_url": git_url or archive_url,
                     "runtime_image": runtime_image}
        if deploy_port:
            step_args["port"] = deploy_port
        out = await ctx.run_tool("deploy_any", step_args, confirmed=True,
                                 progress="Building and starting your "
                                          "application")
    elif source == "archive":
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
                          "now -- the app itself deployed fine.")

    # verify: node readiness -> curl -> did-it-start healthcheck ------------
    if source in ("git", "archive") and tier >= 2:
        await ctx.run_tool("op_result", {
            "env_name": env_name, "op": "nodes_ready", "timeout": 60},
            progress="Waiting for the application node")
    scheme = "https" if ssl else "http"
    app_url = f"{scheme}://{domain}/" if domain else ""
    if app_url:
        verified = await ctx.verify_url(app_url)
        if not verified.ok:
            await handle_failure(ctx, verified,
                                 step_args={"env_name": env_name})
            return
    health = await ctx.run_tool("app_healthcheck",
                                {"env_name": env_name, "context": "ROOT"},
                                progress="Checking the application started")
    if not health.ok or not health.payload.get("ok"):
        await handle_failure(ctx, health, step_args={"env_name": env_name})
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
          "payload": {}},
         {"label": "View logs", "url": "", "action": "open_logs",
          "payload": {}}])

    # security review is the last step of the deploy wizard ---------------
    await security_review(ctx, env_name, wait_for_fix=True)



# --- flow: create environment -----------------------------------------------

async def create_environment(ctx: FlowContext) -> None:
    """Guided 'create an environment' wizard.

    Philosophy (user feedback): MISSING INFORMATION IS NEVER AN ERROR — the
    wizard asks one friendly question per unknown (name, type, size, SSL)
    and only then executes."""
    p = ctx.params

    listed = await ctx.run_tool("envs_list", {})
    existing = {str(e.get("envName")) for e in
                ((listed.payload or {}).get("environments") or [])}

    def _check_name(answer: str) -> Optional[str]:
        name = answer.strip()
        if not ENV_NAME_RE.match(name):
            return ("Please use lowercase letters, digits and dashes "
                    "(must start with a letter, max 30 characters).")
        if name in existing:
            return f"`{name}` is already taken — pick a different name."
        return None

    env_name = slugify_env_name(str(p.get("env_name") or "")) \
        if p.get("env_name") else ""
    if env_name and env_name in existing:
        await ctx.say(f"`{env_name}` is already taken — let's pick another "
                      "name.")
        env_name = ""
    if not env_name:
        env_name = (await ctx.ask(
            "What should the new environment be called?",
            allow_free=True, validate=_check_name)).strip()

    stack = _normalize_stack(str(p.get("stack") or "")) if p.get("stack") \
        else _normalize_stack(await ctx.ask(
            "What should run in this environment?",
            _STACK_OPTIONS))

    db = None
    if p.get("db") is not None:
        db = _normalize_db(str(p["db"])) if p["db"] else None
    else:
        db = _normalize_db(await ctx.ask(
            "Would you like a database with it?",
            ["No database", "MySQL 8.0", "MariaDB 10.11", "PostgreSQL 16",
             "Redis 7.2"]))

    cloudlets = _parse_cloudlets(str(p["cloudlets"])) if p.get("cloudlets") \
        else _parse_cloudlets(await ctx.ask(
            "How much power should it get?",
            ["Dev - 4 cloudlets", "Prod - 12 cloudlets"]), default=8)

    ssl_param = p.get("ssl")
    ssl = _parse_ssl(ssl_param) if ssl_param is not None else \
        _parse_ssl(await ctx.ask(
            "Enable a free Let's Encrypt SSL certificate?",
            ["Yes, enable SSL", "No, skip SSL"]))

    db_part = f" + `{db}`" if db else ""
    recap = (f"Create environment **{env_name}** with `{stack}`{db_part}, "
             f"{cloudlets} cloudlets per node"
             + (", free SSL on." if ssl else ", SSL off."))
    approved = await ctx.confirm(recap, actions=["create environment"])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

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
        await ctx.say("The environment is still starting up — ask me to "
                      "check in a moment.")
        return

    info = await ctx.run_tool("env_info", {"env_name": env_name})
    domain = str((info.payload.get("env") or {}).get("domain") or "")
    if ssl and domain:
        out = await ctx.run_tool("ssl_letsencrypt", {
            "env_name": env_name, "ext_domain": domain}, confirmed=True,
            progress="Issuing a free SSL certificate")
        if not out.ok:
            await ctx.say("The SSL certificate could not be issued right "
                          "now — the environment itself is up.")

    body = (f"- Environment: `{env_name}`\n"
            f"- Stack: `{stack}`" + (f" + `{db}`" if db else "") + "\n"
            f"- Resources: {cloudlets} cloudlets per node\n"
            f"- URL: `{domain or 'pending'}`\n\n"
            f"_Next: deploy an application to it — just say the word._")
    await ctx.card(
        "success", "Environment is up 🎉", body,
        [{"label": "Open dashboard ↗",
          "url": f"{ctx.tools.auth.host}/", "action": "open_url",
          "payload": {}}])


# --- flow 2: add database ---------------------------------------------------

async def db_add(ctx: FlowContext) -> None:
    p = ctx.params

    env_name = slugify_env_name(str(p.get("env_name") or "")) if p.get("env_name") else ""
    if env_name and not await _env_known(ctx, env_name):
        await ctx.say(f"I couldn't find an environment named `{env_name}` — "
                      "let's pick one from your list.")
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


# --- T14 coverage workflows --------------------------------------------------
#
# House style (docs/SCENARIOS.md design rules): one question per ask_user with
# option chips, missing info is NEVER an error, recap -> Confirm -> execute ->
# observable-state verify -> success card with actions; failures go through
# diagnose -> auto-fix (handle_failure) and may never weaken security.

#: custom-domain hostnames (mirrors the tool validator)
HOSTNAME_RE = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

_SUBNET_RE = re.compile(
    r"^([0-9]{1,3}\.){3}[0-9]{1,3}(/(3[0-2]|[12]?[0-9]))?$")


def _normalize_domain(answer: str) -> str:
    """'https://App.Example.com/path' -> 'app.example.com'; '' if invalid."""
    text = (answer or "").strip().lower()
    text = re.sub(r"^[a-z]+://", "", text)
    text = text.split("/")[0].split(":")[0].strip()
    return text if HOSTNAME_RE.match(text) else ""


def _normalize_subnet(answer: str) -> str:
    """'10.0.0.0/8 (private networks)' -> '10.0.0.0/8'; '' if invalid."""
    text = (answer or "").strip().split()[0] if (answer or "").strip() else ""
    return text if _SUBNET_RE.match(text) else ""


def _normalize_protocol(answer: str) -> str:
    return "UDP" if "udp" in (answer or "").lower() else "TCP"


def _parse_port(answer) -> Optional[int]:
    found = re.search(r"\d+", str(answer or ""))
    if not found:
        return None
    value = int(found.group(0))
    return value if 1 <= value <= 65535 else None


async def _resolve_env(ctx: FlowContext, question: str,
                       param: str = "env_name") -> str:
    """Target environment from params, or ONE picker question (chips)."""
    env_name = slugify_env_name(str(ctx.params.get(param) or "")) \
        if ctx.params.get(param) else ""
    if env_name and not await _env_known(ctx, env_name):
        await ctx.say(f"I couldn't find an environment named `{env_name}` — "
                      "let's pick one from your list.")
        env_name = ""
    if not env_name:
        listed = await ctx.run_tool("envs_list", {})
        names = [str(e.get("envName")) for e in
                 ((listed.payload or {}).get("environments") or [])]
        if not names:
            await ctx.say("There are no environments on this account yet — "
                          "create one first and I will come back to this.")
            return ""
        env_name = match_option(await ctx.ask(question, names), names)
    return env_name


def _node_label(node: dict) -> str:
    return (f"{node.get('displayName') or node.get('nodeType') or 'node'} "
            f"(node {node.get('id')})")


async def _resolve_node(ctx: FlowContext, env_name: str, question: str,
                        param: str = "node_id") -> Optional[dict]:
    """One node of the environment from params, or ONE picker question."""
    info = await ctx.run_tool("env_info", {"env_name": env_name})
    nodes = [n for n in (((info.payload or {}).get("env") or {}).get("nodes")
                         or []) if n.get("id") is not None]
    wanted = ctx.params.get(param)
    if wanted is not None:
        match = next((n for n in nodes if str(n.get("id")) == str(wanted)),
                     None)
        if match is not None:
            return match
    if not nodes:
        await ctx.say(f"`{env_name}` has no nodes to act on.")
        return None
    labels = [_node_label(n) for n in nodes]

    def _check(answer: str) -> Optional[str]:
        return None if re.search(r"node\s+\d+", answer) else \
            "Please pick one of the nodes listed."

    answer = await ctx.ask(question, labels, validate=_check)
    found = re.search(r"node\s+(\d+)", answer)
    if found:
        return next((n for n in nodes if str(n.get("id")) == found.group(1)),
                    nodes[0])
    return nodes[0]


async def _pick_node_group(ctx: FlowContext, env_name: str,
                           param: str = "node_group") -> str:
    groups = str(ctx.params.get(param) or "").strip()
    if groups:
        return groups
    info = await ctx.run_tool("env_info", {"env_name": env_name})
    names = sorted({str(n.get("nodeGroup") or "cp") for n in
                    (((info.payload or {}).get("env") or {}).get("nodes")
                     or [])}) or ["cp"]
    return match_option(await ctx.ask(
        "Which node group should scale?", names), names)


# --- flow: domains (connect / detach custom domains + SSL) -------------------

async def domain_setup(ctx: FlowContext) -> None:
    """Connect a custom domain to an environment (rows 7-8).

    pick env -> ask domain (free text, hostname-validated) -> optional
    Let's Encrypt SSL -> recap -> Confirm -> bind -> verify (env_info shows
    the binding; verify_url checks DNS and prints a hint when the domain
    does not resolve yet) -> success card (Open app / Open dashboard).
    """
    env_name = await _resolve_env(
        ctx, "Which environment should I connect the domain to?")
    if not env_name:
        return

    domain = _normalize_domain(str(ctx.params.get("domain")
                                   or ctx.params.get("ext_domain") or ""))
    if not domain:
        domain = _normalize_domain(await ctx.ask(
            "What domain would you like to connect? (e.g. `app.example.com`)",
            allow_free=True,
            validate=lambda a: (None if _normalize_domain(a) else
                                "Please give a valid domain name, e.g. "
                                "`app.example.com`.")))

    ssl = _parse_ssl(ctx.params["ssl"]) if ctx.params.get("ssl") is not None \
        else _parse_ssl(await ctx.ask(
            "Should I also issue a free Let's Encrypt SSL certificate "
            "(HTTPS)?", ["Yes, enable HTTPS", "No, just connect the domain"]))

    recap = (f"Connect **{domain}** to `{env_name}`"
             + (" and issue a free Let's Encrypt SSL certificate (HTTPS)."
                if ssl else " (no SSL).")
             + " DNS must point the domain at the platform for it to open.")
    approved = await ctx.confirm(
        recap, actions=(["connect domain", "issue SSL certificate"]
                        if ssl else ["connect domain"]))
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    step_args = {"env_name": env_name, "ext_domain": domain, "ssl": ssl}
    out = await ctx.run_tool("domain_bind", step_args, confirmed=True,
                             progress="Connecting the domain")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args,
                                            retry_tool="domain_bind")
        if out is None or not out.ok:
            return

    # observable-state verify: the binding appears in env_info ...
    info = await ctx.run_tool("env_info", {"env_name": env_name})
    bound = [d for d in (((info.payload or {}).get("env") or {})
                         .get("extDomains") or [])
             if str(d.get("extDomain") or "").lower() == domain]
    # ... and the URL check tells us whether DNS has caught up (not
    # resolving right after a bind is expected, never an error).
    app_url = f"{'https' if ssl else 'http'}://{domain}/"
    checked = await ctx.verify_url(app_url)

    body = (f"- Environment: `{env_name}`\n"
            f"- Domain: `{domain}`\n"
            "- SSL: " + ("Let's Encrypt (HTTPS)" if ssl else "off") + "\n")
    if not bound:
        body += ("\nThe platform has not confirmed the binding yet — ask me "
                 "to re-check in a moment.")
    if checked.ok:
        body += f"\nVerified: `{app_url}` answers — the domain is live."
    else:
        body += (f"\n**DNS hint:** `{domain}` does not resolve to the "
                 "platform yet. Point its DNS (an A or CNAME record) at the "
                 "platform and allow up to a few minutes for propagation — "
                 "the binding itself is already active.")
    await ctx.card("success", "Domain connected", body,
                   [{"label": "Open app", "url": app_url,
                     "action": "open_url", "payload": {}},
                    {"label": "Open dashboard",
                     "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                     "payload": {}}])


async def domain_unbind(ctx: FlowContext) -> None:
    """Detach a custom domain from an environment (row 7, closing path)."""
    env_name = await _resolve_env(
        ctx, "Which environment should I detach the domain from?")
    if not env_name:
        return
    info = await ctx.run_tool("env_info", {"env_name": env_name})
    bound = [str(d.get("extDomain")) for d in
             (((info.payload or {}).get("env") or {}).get("extDomains") or [])]

    domain = _normalize_domain(str(ctx.params.get("domain")
                                   or ctx.params.get("ext_domain") or ""))
    if not domain:
        domain = _normalize_domain(await ctx.ask(
            "Which domain should I detach?", bound, allow_free=True,
            validate=lambda a: (None if _normalize_domain(a) else
                                "Please give a valid domain name, e.g. "
                                "`app.example.com`.")))

    recap = (f"Detach **{domain}** from `{env_name}` — the domain will stop "
             "opening this environment.")
    approved = await ctx.confirm(recap, actions=["detach domain"])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    step_args = {"env_name": env_name, "ext_domain": domain}
    out = await ctx.run_tool("domain_unbind", step_args, confirmed=True,
                             progress="Detaching the domain")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args,
                                            retry_tool="domain_unbind")
        if out is None or not out.ok:
            return

    info = await ctx.run_tool("env_info", {"env_name": env_name})
    still = [d for d in (((info.payload or {}).get("env") or {})
                         .get("extDomains") or [])
             if str(d.get("extDomain") or "").lower() == domain]
    body = (f"- Environment: `{env_name}`\n- Domain: `{domain}`\n\n"
            + ("The domain is detached." if not still else
               "The platform still lists the domain — ask me to re-check "
               "in a moment."))
    await ctx.card("success", "Domain detached", body,
                   [{"label": "Open dashboard",
                     "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                     "payload": {}}])


# --- flow: clone environment -------------------------------------------------

async def clone_env(ctx: FlowContext) -> None:
    """Clone an environment (row 9): source -> new name -> recap -> Confirm
    -> clone -> wait ready (op_result) -> success card."""
    source = await _resolve_env(ctx, "Which environment should I clone?")
    if not source:
        return
    listed = await ctx.run_tool("envs_list", {})
    existing = {str(e.get("envName")) for e in
                ((listed.payload or {}).get("environments") or [])}

    def _check_name(answer: str) -> Optional[str]:
        name = answer.strip()
        if not ENV_NAME_RE.match(name):
            return ("Please use lowercase letters, digits and dashes "
                    "(must start with a letter, max 30 characters).")
        if name in existing:
            return f"`{name}` is already taken — pick a different name."
        return None

    new_name = slugify_env_name(str(ctx.params.get("new_name") or "")) \
        if ctx.params.get("new_name") else ""
    if new_name and new_name in existing:
        await ctx.say(f"`{new_name}` is already taken — let's pick another "
                      "name.")
        new_name = ""
    if not new_name:
        new_name = (await ctx.ask(
            "What should the clone be called?", allow_free=True,
            validate=_check_name)).strip()

    recap = (f"Clone `{source}` to **{new_name}**: a full copy of the "
             "topology (nodes and their sizes), data, configuration, "
             "environment variables, triggers and scheduled tasks. The "
             "clone is a separate environment — the original is not "
             "touched.")
    approved = await ctx.confirm(recap, actions=["clone environment"])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    step_args = {"env_name": source, "new_name": new_name}
    out = await ctx.run_tool("env_clone", step_args, confirmed=True,
                             progress="Cloning the environment")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args,
                                            retry_tool="env_clone")
        if out is None or not out.ok:
            return

    ready = await ctx.run_tool("op_result", {
        "env_name": new_name, "op": "status", "target": ["running"],
        "timeout": 60}, progress="Waiting for the clone to start")
    if not ready.ok or ready.payload.get("status") != "done":
        await ctx.say(f"The clone `{new_name}` is still being assembled — "
                      "ask me to check in a moment.")
        return
    info = await ctx.run_tool("env_info", {"env_name": new_name})
    domain = str(((info.payload or {}).get("env") or {}).get("domain") or "")
    body = (f"- Source: `{source}`\n"
            f"- Clone: `{new_name}`\n"
            f"- URL: `{domain or 'pending'}`\n\n"
            f"_Verified: the clone is up and running._")
    actions = [{"label": "Open dashboard", "url": f"{ctx.tools.auth.host}/",
                "action": "open_url", "payload": {}}]
    if domain:
        actions.insert(0, {"label": "Open clone",
                           "url": f"https://{domain}/", "action": "open_url",
                           "payload": {}})
    await ctx.card("success", "Clone ready", body, actions)


# --- flow: auto-scaling triggers ---------------------------------------------

_METRIC_TOKENS = (("cpu", "CPU"), ("ram", "RAM"), ("mem", "RAM"),
                  ("disk", "DISK"), ("request", "REQUESTS"),
                  ("traffic", "REQUESTS"))


def _normalize_metric(answer: str) -> str:
    lowered = (answer or "").lower()
    for token, metric in _METRIC_TOKENS:
        if token in lowered:
            return metric
    return "CPU"


def _parse_thresholds(answer: str) -> Optional[Tuple[int, Optional[int]]]:
    """'Scale up at 80%, scale down at 40% (busy)' -> (80, 40)."""
    numbers = [int(n) for n in re.findall(r"\d+", answer or "")]
    numbers = [n for n in numbers if 0 <= n <= 100]
    if not numbers:
        return None
    up = numbers[0]
    down = numbers[1] if len(numbers) > 1 and numbers[1] < up else None
    return up, down


def _parse_step(answer) -> int:
    found = re.search(r"\d+", str(answer or ""))
    value = int(found.group(0)) if found else 1
    return value if 1 <= value <= 10 else 1


async def autoscale_setup(ctx: FlowContext) -> None:
    """Auto horizontal scaling (row 14): env -> node group -> metric ->
    thresholds -> step -> plain-language 'what will happen' -> Confirm ->
    trigger_create -> verify (trigger_list) -> success card. Also the
    trigger management entry point is :func:`trigger_manage`."""
    env_name = await _resolve_env(ctx, "Which environment should auto-scale?")
    if not env_name:
        return
    group = await _pick_node_group(ctx, env_name)

    metric = str(ctx.params.get("metric") or "").strip()
    metric = _normalize_metric(metric if metric else await ctx.ask(
        "Which metric should trigger scaling?",
        ["CPU (processor load)", "RAM (memory)", "Disk (storage)",
         "Requests (traffic)"]))

    thresholds = _parse_thresholds(str(ctx.params.get("thresholds") or "")) \
        if ctx.params.get("thresholds") else None
    if thresholds is None and ctx.params.get("scale_up_threshold") is not None:
        up = _parse_cloudlets(str(ctx.params["scale_up_threshold"]), default=0)
        down = _parse_cloudlets(str(ctx.params.get("scale_down_threshold")
                                    or ""), default=0) or None
        thresholds = (up, down if down and down < up else None)
    if thresholds is None:
        thresholds = _parse_thresholds(await ctx.ask(
            "When should it scale?",
            ["Scale up at 70%, scale down at 30% (balanced)",
             "Scale up at 80%, scale down at 40% (busy)",
             "Scale up at 90% (spikes only)"])) or (70, None)
    up, down = thresholds

    step = _parse_step(ctx.params.get("step")) if ctx.params.get("step") \
        else _parse_step(await ctx.ask(
            "How many nodes should be added per step?",
            ["Add 1 node at a time", "Add 2 nodes at a time"]))

    what = (f"When **{metric}** on the `{group}` nodes stays above **{up}%** "
            f"for 5 minutes, I add {step} node(s) automatically.")
    if down is not None:
        what += (f" When it stays below **{down}%**, I remove them again.")
    what += (" That keeps the app responsive under load and saves money when "
             "the load drops.")
    recap = (f"{what}\n\nEnvironment: `{env_name}`, node group: `{group}`.")
    approved = await ctx.confirm(recap, actions=["create scaling trigger"])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    step_args = {"env_name": env_name, "node_group": group, "metric": metric,
                 "scale_up_threshold": up, "step": step, "period": 5}
    if down is not None:
        step_args["scale_down_threshold"] = down
    out = await ctx.run_tool("trigger_create", step_args, confirmed=True,
                             progress="Creating the scaling trigger")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args,
                                            retry_tool="trigger_create")
        if out is None or not out.ok:
            return

    listed = await ctx.run_tool("trigger_list", {"env_name": env_name})
    count = (listed.payload or {}).get("count")
    body = (f"- Environment: `{env_name}`\n"
            f"- Node group: `{group}`\n"
            f"- Rule: {what}\n\n"
            + (f"Verified: {count} scaling rule(s) active on the "
               f"environment." if listed.ok else
               "Ask me to re-check the scaling rules in a moment."))
    await ctx.card("success", "Auto-scaling is on", body,
                   [{"label": "Open dashboard",
                     "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                     "payload": {}}])


async def trigger_manage(ctx: FlowContext) -> None:
    """List / remove auto-scaling triggers (row 14, management path)."""
    env_name = await _resolve_env(
        ctx, "Which environment's scaling rules should I manage?")
    if not env_name:
        return
    listed = await ctx.run_tool("trigger_list", {"env_name": env_name})
    triggers = (listed.payload or {}).get("triggers") or []
    if not triggers:
        await ctx.card(
            "info", "No scaling rules yet",
            f"`{env_name}` has no auto-scaling triggers. Say **set up "
            "auto-scaling** and I will create one.", [])
        return

    await ctx.say("Current scaling rules:\n" + "\n".join(
        f"- `#{t.get('id')}`: {t.get('type')} {t.get('condition')} "
        f"{t.get('value')}% → {t.get('action')} {t.get('amount')} node(s)"
        for t in triggers))
    labels = [f"#{t.get('id')}: {t.get('type')} {t.get('condition')} "
              f"{t.get('value')} → {t.get('action')} {t.get('amount')}"
              for t in triggers]
    choice = await ctx.ask("Which rule should I remove?", labels +
                           ["Keep them all"], allow_free=True)
    found = re.search(r"#?(\d+)", choice)
    if found is None or "keep" in choice.lower():
        await ctx.say("Understood — I changed nothing.")
        return
    trigger_id = int(found.group(1))

    recap = (f"Remove scaling rule `#{trigger_id}` from `{env_name}` — the "
             "environment will no longer scale automatically because of it.")
    approved = await ctx.confirm(recap, actions=["remove scaling trigger"])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    step_args = {"env_name": env_name, "trigger_id": trigger_id}
    out = await ctx.run_tool("trigger_delete", step_args, confirmed=True,
                             progress="Removing the scaling trigger")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args,
                                            retry_tool="trigger_delete")
        if out is None or not out.ok:
            return

    listed = await ctx.run_tool("trigger_list", {"env_name": env_name})
    still = [t for t in ((listed.payload or {}).get("triggers") or [])
             if str(t.get("id")) == str(trigger_id)]
    body = (f"- Environment: `{env_name}`\n"
            f"- Removed rule: `#{trigger_id}`\n\n"
            + ("Verified: the rule is gone." if not still else
               "The platform still lists the rule — ask me to re-check "
               "in a moment."))
    await ctx.card("success", "Scaling rule removed", body,
                   [{"label": "Open dashboard",
                     "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                     "payload": {}}])


# --- flow: scheduled tasks ---------------------------------------------------

_CRON_PRESETS = (
    ("every night at 2 am", "0 2 * * *", "every night at 02:00"),
    ("every hour", "0 * * * *", "every hour on the hour"),
    ("weekdays at 9 am", "0 9 * * 1-5", "weekdays at 09:00"),
)


def _parse_cron(answer: str) -> Optional[str]:
    text = (answer or "").strip().lower()
    for token, cron, _human in _CRON_PRESETS:
        if token in text:
            return cron
    fields = text.split()
    if len(fields) == 5 and all(re.fullmatch(r"[0-9*/, \-]+", f)
                                for f in fields):
        return " ".join(fields)
    return None


def _cron_human(cron: str) -> str:
    for _token, preset, human in _CRON_PRESETS:
        if preset == cron:
            return human
    return f"on the schedule `{cron}`"


_TASK_ACTIONS = (("restart", "restart"), ("scale up", "scale up"),
                 ("scale down", "scale down"))


def _normalize_task_action(answer: str) -> str:
    lowered = (answer or "").lower()
    for token, action in _TASK_ACTIONS:
        if token in lowered:
            return action
    return (answer or "").strip()


def _command_ok(answer: str) -> Optional[str]:
    text = (answer or "").strip()
    return None if text and re.fullmatch(r"[A-Za-z0-9 ._/:,@%+=,-]{1,256}",
                                         text) else \
        "Please give a simple command, e.g. `restart` or `/opt/backup.sh`."


async def schedule_task(ctx: FlowContext) -> None:
    """Scheduled task (row 15): env -> what to do -> when (cron presets +
    free cron) -> recap -> Confirm -> task_create -> verify (task_list) ->
    success card with a 'next run' explanation."""
    env_name = await _resolve_env(
        ctx, "Which environment should the task run on?")
    if not env_name:
        return

    action = str(ctx.params.get("action") or "").strip()
    if not action:
        choice = await ctx.ask(
            "What should the scheduled task do?",
            ["Restart the environment", "Scale up", "Scale down",
             "Custom command (I'll type it)"])
        if "custom" in choice.lower():
            action = (await ctx.ask(
                "What command should the task run?", allow_free=True,
                validate=_command_ok)).strip()
        else:
            action = _normalize_task_action(choice)

    cron = _parse_cron(str(ctx.params.get("cron") or "")) if \
        ctx.params.get("cron") else None
    if cron is None:
        answer = await ctx.ask(
            "When should it run?",
            ["Every night at 2 AM", "Every hour", "Weekdays at 9 AM"])
        cron = _parse_cron(answer)
        while cron is None:
            answer = await ctx.ask(
                "I could not read that schedule — please give a five-field "
                "cron expression (minute hour day month weekday), e.g. "
                "`0 2 * * *`.", allow_free=True)
            cron = _parse_cron(answer)

    human = _cron_human(cron)
    recap = (f"Schedule **{action}** on `{env_name}` — {human} (server "
             "time).")
    approved = await ctx.confirm(recap, actions=["create scheduled task"])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    step_args = {"env_name": env_name, "cron": cron, "action": action}
    out = await ctx.run_tool("task_create", step_args, confirmed=True,
                             progress="Creating the scheduled task")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args,
                                            retry_tool="task_create")
        if out is None or not out.ok:
            return

    listed = await ctx.run_tool("task_list", {"env_name": env_name})
    count = (listed.payload or {}).get("count")
    body = (f"- Environment: `{env_name}`\n"
            f"- Task: `{action}`\n"
            f"- Schedule: `{cron}` — {human}\n\n"
            f"**Next run:** the task runs {human}, at the next matching "
            f"time in the **server's** timezone.\n\n"
            + (f"Verified: {count} scheduled task(s) on the environment."
               if listed.ok else
               "Ask me to re-check the schedule in a moment."))
    await ctx.card("success", "Task scheduled", body,
                   [{"label": "Open dashboard",
                     "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                     "payload": {}}])


# --- flow: share access ------------------------------------------------------

_ROLE_SUMMARIES = {
    "admin": "full control — including deleting the environment and "
             "changing everything about it",
    "user": "deploy and manage applications, but not delete the "
            "environment",
    "viewer": "read-only access — see everything, change nothing",
}


def _normalize_role(answer: str) -> str:
    lowered = (answer or "").lower()
    if "admin" in lowered:
        return "admin"
    if "view" in lowered or "read" in lowered:
        return "viewer"
    return "user"


async def share_env(ctx: FlowContext) -> None:
    """Share an environment with a teammate (row 16): env -> email -> role
    chips -> plain-language permission summary -> Confirm -> env_share ->
    verify (env_share_list) -> success card."""
    env_name = await _resolve_env(
        ctx, "Which environment would you like to share?")
    if not env_name:
        return

    email = str(ctx.params.get("email") or "").strip().lower()
    if not EMAIL_RE.match(email):
        email = (await ctx.ask(
            "Whose account should get access? (their account email)",
            allow_free=True,
            validate=lambda a: (None if EMAIL_RE.match(a.strip()) else
                                "Please give a valid email address, e.g. "
                                "`teammate@example.com`."))).strip().lower()

    role = _normalize_role(str(ctx.params.get("role") or "")) if \
        ctx.params.get("role") else _normalize_role(await ctx.ask(
            "What should they be able to do?",
            ["Admin — full control over the environment",
             "User — deploy and manage applications",
             "Viewer — read-only access"]))

    await ctx.say(f"As **{role}**, `{email}` gets {_ROLE_SUMMARIES[role]}.")
    recap = (f"Share `{env_name}` with **{email}** as **{role}**. They "
             "receive an invitation and must accept it before access "
             "starts.")
    approved = await ctx.confirm(recap, actions=["send invitation"])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    step_args = {"env_name": env_name, "email": email, "role": role}
    out = await ctx.run_tool("env_share", step_args, confirmed=True,
                             progress="Sending the invitation")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args,
                                            retry_tool="env_share")
        if out is None or not out.ok:
            return

    listed = await ctx.run_tool("env_share_list", {"env_name": env_name})
    members = (listed.payload or {}).get("members") or []
    joined = [m for m in members if str(m.get("email") or "").lower() == email]
    body = (f"- Environment: `{env_name}`\n"
            f"- Invited: `{email}` as **{role}**\n"
            f"- Access: {_ROLE_SUMMARIES[role]}\n\n"
            + ("Verified: the invitation is on the member list. They must "
               "accept it before access starts." if joined else
               "The member list does not show the invitation yet — ask me "
               "to re-check in a moment."))
    await ctx.card("success", "Invitation sent", body,
                   [{"label": "Open dashboard",
                     "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                     "payload": {}}])


# --- flow: usage / bill report ----------------------------------------------

async def usage_report(ctx: FlowContext) -> None:
    """Billing & quotas (row 17): quota_get + usage_list -> human summary
    ('what you use, what it costs') as a readable table + one insight.
    Read-only: no questions, no confirmation. The 'Explain my bill' chip
    routes here."""
    quota = await ctx.run_tool("quota_get", {}, progress="Reading your quotas")
    usage = await ctx.run_tool("usage_list", {}, progress="Reading your usage")
    rows = (usage.payload or {}).get("rows") or []
    total = (usage.payload or {}).get("total_cost")
    quotas = (quota.payload or {}).get("quotas") or {}

    lines = ["Here is what you use and what it costs.", "",
             "| Environment | Nodes | Cloudlets | Cost |",
             "|---|---|---|---|"]
    for row in rows:
        lines.append(f"| `{row.get('envName')}` | {row.get('nodes')} | "
                     f"{row.get('cloudlets')} | ${row.get('cost')} |")
    if rows:
        lines.append(f"| **Total** | | | **${total}** |")
    else:
        lines.append("| _no usage recorded yet_ | | | |")

    insight = ""
    if rows:
        priciest = max(rows, key=lambda r: float(r.get("cost") or 0))
        insight = (f"\n**One insight:** `{priciest.get('envName')}` is your "
                   f"biggest cost at **${priciest.get('cost')}** — it runs "
                   f"{priciest.get('cloudlets')} cloudlets across "
                   f"{priciest.get('nodes')} node(s). Stopping or shrinking "
                   f"it when it is idle is the fastest way to spend less.")
    quota_bits = []
    for key, label in (("environments", "environments"),
                       ("nodes", "nodes"), ("cloudlets", "cloudlets")):
        entry = quotas.get(key)
        if isinstance(entry, dict):
            quota_bits.append(f"{entry.get('usage')}/{entry.get('limit')} "
                              f"{label}")
    quota_line = (f"\n\n**Your plan:** {', '.join(quota_bits)} in use."
                  if quota_bits else "")

    await ctx.card("info", "Your usage and costs",
                   "\n".join(lines) + insight + quota_line,
                   [{"label": "Open dashboard",
                     "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                     "payload": {}}])


# --- flows: public ports (open/close) ----------------------------------------

async def open_port(ctx: FlowContext) -> None:
    """Open a public TCP/UDP endpoint (row 11): env -> node -> port ->
    protocol -> INTERNET EXPOSURE recap -> Confirm -> endpoint_open ->
    warning surfaced -> verify (env_info) -> success card."""
    env_name = await _resolve_env(
        ctx, "Which environment needs the open port?")
    if not env_name:
        return
    node = await _resolve_node(ctx, env_name,
                              "Which node should the port open on?")
    if node is None:
        return
    node_id = node.get("id")

    port = _parse_port(ctx.params.get("port")) if ctx.params.get("port") \
        else None
    if port is None:
        port = _parse_port(await ctx.ask(
            "Which port number should be opened?", allow_free=True,
            validate=lambda a: (None if _parse_port(a) else
                                "Please give a port number between 1 and "
                                "65535.")))
    protocol = _normalize_protocol(str(ctx.params.get("protocol") or "")) \
        if ctx.params.get("protocol") else _normalize_protocol(
            await ctx.ask("Which protocol?", ["TCP", "UDP"]))

    recap = (f"⚠️ **INTERNET EXPOSURE** — this opens **{protocol}/{port}** "
             f"on node `{node_id}` of `{env_name}` to the **whole "
             "Internet**. Anyone can reach that port; make sure the "
             "service behind it is meant to be public and is protected.")
    approved = await ctx.confirm(recap, actions=["open the public port"])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    step_args = {"env_name": env_name, "node_id": node_id, "port": port,
                 "protocol": protocol}
    out = await ctx.run_tool("endpoint_open", step_args, confirmed=True,
                             progress="Opening the public port")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args,
                                            retry_tool="endpoint_open")
        if out is None or not out.ok:
            return

    await ctx.run_tool("env_info", {"env_name": env_name},
                       progress="Re-checking the environment")
    warning = str((out.payload or {}).get("warning") or "")
    body = (f"- Environment: `{env_name}`\n"
            f"- Endpoint: `{protocol}/{port}` on node `{node_id}`\n"
            f"- Reachable from: the Internet\n")
    if warning:
        body += f"\n**Warning:** {warning}\n"
    body += "\nClose it any time — just say *close the port*."
    await ctx.card("success", "Port is open", body,
                   [{"label": "Open dashboard",
                     "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                     "payload": {}}])


async def close_port(ctx: FlowContext) -> None:
    """Close a public TCP/UDP endpoint (row 11, closing counterpart)."""
    env_name = await _resolve_env(
        ctx, "Which environment should the port close on?")
    if not env_name:
        return
    node = await _resolve_node(ctx, env_name,
                              "Which node is the port open on?")
    if node is None:
        return
    node_id = node.get("id")

    port = _parse_port(ctx.params.get("port")) if ctx.params.get("port") \
        else None
    if port is None:
        port = _parse_port(await ctx.ask(
            "Which port number should be closed?", allow_free=True,
            validate=lambda a: (None if _parse_port(a) else
                                "Please give a port number between 1 and "
                                "65535.")))
    protocol = _normalize_protocol(str(ctx.params.get("protocol") or "")) \
        if ctx.params.get("protocol") else _normalize_protocol(
            await ctx.ask("Which protocol?", ["TCP", "UDP"]))

    recap = (f"Close **{protocol}/{port}** on node `{node_id}` of "
             f"`{env_name}` — anything using it loses public access.")
    approved = await ctx.confirm(recap, actions=["close the public port"])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    step_args = {"env_name": env_name, "node_id": node_id, "port": port,
                 "protocol": protocol}
    out = await ctx.run_tool("endpoint_close", step_args, confirmed=True,
                             progress="Closing the public port")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args,
                                            retry_tool="endpoint_close")
        if out is None or not out.ok:
            return

    await ctx.run_tool("env_info", {"env_name": env_name},
                       progress="Re-checking the environment")
    warning = str((out.payload or {}).get("warning") or "")
    body = (f"- Environment: `{env_name}`\n"
            f"- Closed: `{protocol}/{port}` on node `{node_id}`\n")
    if warning:
        body += f"\n**Note:** {warning}\n"
    await ctx.card("success", "Port is closed", body,
                   [{"label": "Open dashboard",
                     "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                     "payload": {}}])


# --- flow: container firewall rules ------------------------------------------

async def firewall_manage(ctx: FlowContext) -> None:
    """Container firewall rules (row 12): add/remove, both gated like the
    endpoint flow with a plain-language FIREWALL CHANGE recap."""
    env_name = await _resolve_env(
        ctx, "Which environment's firewall should I change?")
    if not env_name:
        return
    node = await _resolve_node(ctx, env_name,
                              "Which node is the rule for?")
    if node is None:
        return
    node_id = node.get("id")

    intent = str(ctx.params.get("action") or "").strip()
    removing = "remove" in intent.lower()
    if not intent:
        choice = await ctx.ask(
            "What should the firewall do?",
            ["Block traffic (DROP)", "Allow traffic (ACCEPT)",
             "Remove an existing rule"])
        removing = "remove" in choice.lower()
        intent = choice

    direction = "DROP" if ("drop" in intent.lower()
                           or "block" in intent.lower()) else "ACCEPT"
    if removing and not any(token in intent.lower() for token in
                            ("drop", "accept", "block", "allow")):
        direction = "DROP" if "drop" in (await ctx.ask(
            "Which rule should I remove?",
            ["DROP (block) rule", "ACCEPT (allow) rule"])).lower() \
            else "ACCEPT"
    protocol = _normalize_protocol(str(ctx.params.get("protocol") or "")) \
        if ctx.params.get("protocol") else _normalize_protocol(
            await ctx.ask("Which protocol?", ["TCP", "UDP"]))

    port = _parse_port(ctx.params.get("port")) if ctx.params.get("port") \
        else None
    if port is None:
        port = _parse_port(await ctx.ask(
            "Which port does the rule cover?", allow_free=True,
            validate=lambda a: (None if _parse_port(a) else
                                "Please give a port number between 1 and "
                                "65535.")))

    subnet = _normalize_subnet(str(ctx.params.get("subnet") or "")) if \
        ctx.params.get("subnet") else None
    if subnet is None:
        subnet = _normalize_subnet(await ctx.ask(
            "Which source subnet does the rule cover?",
            ["0.0.0.0/0 (everywhere)", "10.0.0.0/8 (private networks)"],
            allow_free=True,
            validate=lambda a: (None if _normalize_subnet(a) else
                                "Please give an IPv4 subnet like "
                                "`10.0.0.0/8`.")))

    verb = "Remove the rule" if removing else f"{direction} the traffic"
    plain = ("" if removing else
             (" This blocks that traffic." if direction == "DROP" else
              " This allows that traffic — including from the Internet for "
              "`0.0.0.0/0`."))
    recap = (f"**Firewall change** — {verb} [{direction} {protocol}/{port} "
             f"from {subnet}] on node `{node_id}` of `{env_name}`.{plain}")
    approved = await ctx.confirm(
        recap, actions=(["remove firewall rule"] if removing
                        else ["add firewall rule"]))
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    tool = "firewall_rule_remove" if removing else "firewall_rule_add"
    step_args = {"env_name": env_name, "node_id": node_id,
                 "direction": direction, "protocol": protocol, "port": port,
                 "subnet": subnet}
    out = await ctx.run_tool(tool, step_args, confirmed=True,
                             progress="Changing the firewall rule")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args,
                                            retry_tool=tool)
        if out is None or not out.ok:
            return

    await ctx.run_tool("env_info", {"env_name": env_name},
                       progress="Re-checking the environment")
    warning = str((out.payload or {}).get("warning") or "")
    body = (f"- Environment: `{env_name}`\n"
            f"- Rule: `{direction} {protocol}/{port} from {subnet}` on node "
            f"`{node_id}`\n"
            f"- Result: {'removed' if removing else 'added'}\n")
    if warning:
        body += f"\n**Warning:** {warning}\n"
    await ctx.card("success",
                   "Firewall rule removed" if removing else
                   "Firewall rule added", body,
                   [{"label": "Open dashboard",
                     "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                     "payload": {}}])


# --- flow: restart -----------------------------------------------------------

async def restart(ctx: FlowContext) -> None:
    """Restart quick-flow (row 10): env -> whole environment or one node ->
    recap -> Confirm -> env_restart / node_restart -> verify (op_result
    nodes_ready + env_info) -> success card."""
    env_name = await _resolve_env(ctx, "Which environment should I restart?")
    if not env_name:
        return

    target = str(ctx.params.get("target") or "").strip()
    node_id = ctx.params.get("node_id")
    if node_id is not None:
        node_id = int(node_id)
    elif not target:
        choice = await ctx.ask("What should I restart?",
                               ["Restart the whole environment",
                                "Restart one node"])
        if "node" in choice.lower() and "whole" not in choice.lower():
            node = await _resolve_node(ctx, env_name,
                                      "Which node should I restart?")
            if node is None:
                return
            node_id = node.get("id")
    elif "node" in target.lower() and "whole" not in target.lower() \
            and node_id is None:
        node = await _resolve_node(ctx, env_name,
                                  "Which node should I restart?")
        if node is None:
            return
        node_id = node.get("id")

    if node_id is not None:
        recap = (f"Restart node `{node_id}` of `{env_name}` — that node is "
                 "briefly unavailable (usually under a minute).")
        tool, step_args = "node_restart", {"env_name": env_name,
                                           "node_id": node_id}
    else:
        recap = (f"Restart `{env_name}` — all nodes restart and the app is "
                 "briefly unavailable (usually under a minute).")
        tool, step_args = "env_restart", {"env_name": env_name}
    approved = await ctx.confirm(recap, actions=["restart"])
    if not approved:
        await ctx.say("Cancelled — I changed nothing.")
        return

    out = await ctx.run_tool(tool, step_args, confirmed=True,
                             progress="Restarting")
    if not out.ok:
        out, applied = await handle_failure(ctx, out, step_args=step_args,
                                            retry_tool=tool)
        if out is None or not out.ok:
            return

    ready = await ctx.run_tool("op_result", {
        "env_name": env_name, "op": "nodes_ready", "timeout": 60},
        progress="Waiting for the nodes to come back")
    info = await ctx.run_tool("env_info", {"env_name": env_name},
                              progress="Verifying the environment")
    status = str(((info.payload or {}).get("env") or {}).get("statusName")
                 or "")
    body = (f"- Environment: `{env_name}`\n"
            f"- Restarted: {'node `' + str(node_id) + '`' if node_id is not None else 'the whole environment'}\n"
            f"- Status: **{status or 'unknown'}**")
    if not ready.ok or ready.payload.get("status") != "done":
        body += ("\n\nSome nodes are still coming back — ask me to "
                 "re-check in a moment.")
    await ctx.card("success", "Restart complete", body,
                   [{"label": "Open dashboard",
                     "url": f"{ctx.tools.auth.host}/", "action": "open_url",
                     "payload": {}}])


# --- dispatcher -------------------------------------------------------------

FLOWS = {
    "deploy_app": deploy_app,
    "env_create": create_environment,
    "db_add": db_add,
    "scale": scale,
    "troubleshoot": troubleshoot,
    "security_review": None,   # special-cased below (needs env_name)
    # T14 coverage workflows (docs/SCENARIOS.md rows 7-17)
    "domain_setup": domain_setup,
    "domain_unbind": domain_unbind,
    "clone_env": clone_env,
    "autoscale_setup": autoscale_setup,
    "trigger_manage": trigger_manage,
    "schedule_task": schedule_task,
    "share_env": share_env,
    "usage_report": usage_report,
    "open_port": open_port,
    "close_port": close_port,
    "firewall_manage": firewall_manage,
    "restart": restart,
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
