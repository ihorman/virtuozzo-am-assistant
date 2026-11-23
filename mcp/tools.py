"""MCP tool surface for VAP Copilot: specs, safety gates, execution.

The tool list follows docs/PLAN.md section 2 (``envs_list`` .. ``op_result``).
Every tool runs with the *caller's own* platform session (auth passthrough:
the server never stores credentials), and every mutating tool is gated:

- **``confirm=true``** (JSON boolean) is required on every mutating tool.
  Anything else (missing, ``"true"``, ``1``) is refused *before* any platform
  call happens.
- **``env_delete``** additionally requires the account ``password``, which is
  forwarded once to ``deleteenv`` and never stored or echoed.

Execution errors come back as MCP tool results with ``isError: true`` (per the
MCP spec, tool failures are in-band results, not JSON-RPC errors); a deadline
hit is *not* an error — Jelastic operations are fire-and-forget, so a timeout
returns ``status: "in_progress"`` with the last observed state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Tuple

from .jelastic_client import (
    AuthFailedError,
    DeleteInProgressError,
    DomainUnavailableError,
    EnvironmentNotFoundError,
    InvalidParameterError,
    JelasticClient,
    JelasticError,
    MalformedResponseError,
    ManifestError,
    NodeNotFoundError,
    OperationFailedError,
    OperationTimeout,
    SessionExpiredError,
)

# --- auth passthrough -------------------------------------------------------


class NoCredentialsError(Exception):
    """Neither the HTTP headers nor the stdio env vars carry platform auth."""


@dataclass(frozen=True)
class AuthConfig:
    """Where and as whom to call the platform. Held per request, never stored."""

    host: str
    session: str

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> "AuthConfig":
        """HTTP mode: X-Jelastic-Host + X-Jelastic-Session (case-insensitive)."""
        lowered = {str(k).lower(): v for k, v in headers.items()}
        host = lowered.get("x-jelastic-host")
        session = lowered.get("x-jelastic-session")
        if not host or not session:
            raise NoCredentialsError(
                "X-Jelastic-Host and X-Jelastic-Session headers are required")
        return cls(host=host, session=session)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "AuthConfig":
        """stdio mode: JELASTIC_HOST + JELASTIC_SESSION environment variables."""
        env = os.environ if env is None else env
        host = env.get("JELASTIC_HOST")
        session = env.get("JELASTIC_SESSION")
        if not host or not session:
            raise NoCredentialsError(
                "JELASTIC_HOST and JELASTIC_SESSION environment variables "
                "are required")
        return cls(host=host, session=session)


# --- input helpers ----------------------------------------------------------


class ToolInputError(Exception):
    """Tool arguments are missing or malformed (maps to an in-band error)."""


def _req_str(args: dict, name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"argument [{name}] must be a non-empty string")
    return value.strip()


def _opt_str(args: dict, name: str) -> Optional[str]:
    value = args.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolInputError(f"argument [{name}] must be a string")
    return value.strip() or None


def _req_int(args: dict, name: str) -> int:
    value = args.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolInputError(f"argument [{name}] must be an integer")
    return int(value)


def _opt_int(args: dict, name: str) -> Optional[int]:
    if args.get(name) is None:
        return None
    return _req_int(args, name)


def _cloudlet_split(total: int) -> dict:
    """Wizard cloudlet preset -> fixed/flexible split (fixed 1, rest flexible)."""
    if total < 2:
        raise ToolInputError(
            "argument [cloudlets] must be at least 2 (1 fixed + flexible)")
    return {"fixedCloudlets": 1, "flexibleCloudlets": total - 1}


def _stack_node(stack: str, node_group: str,
                cloudlets: Optional[int]) -> dict:
    """'php:8.3' -> node spec with nodeType 'php', displayName 'php:8.3'."""
    base = stack.split(":", 1)[0].strip()
    if not base:
        raise ToolInputError(f"invalid stack/db spec [{stack}]")
    spec = {"nodeType": base, "nodeGroup": node_group,
            "displayName": stack, "count": 1}
    if cloudlets is not None:
        spec.update(_cloudlet_split(cloudlets))
    return spec


# --- tool specs -------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable  # async (JelasticClient, dict) -> payload dict

    @property
    def mutating(self) -> bool:
        """Single source of truth: a mutating tool requires ``confirm``."""
        return "confirm" in self.input_schema.get("required", [])

    @property
    def annotations(self) -> dict:
        return {
            "title": self.name.replace("_", " ").title(),
            "readOnlyHint": not self.mutating,
            "destructiveHint": self.mutating,
            "idempotentHint": self.name in ("env_stop", "env_sleep", "env_delete",
                                            "env_var_set", "topology_change"),
            "openWorldHint": False,
        }

    def to_mcp(self) -> dict:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema, "annotations": self.annotations}


def _schema(properties: dict, required=(), *, mutating: bool) -> dict:
    properties = dict(properties)
    required_l: list = list(required)
    if mutating:
        properties["confirm"] = {
            "type": "boolean",
            "description": (
                "Safety gate. Must be the JSON boolean true, and ONLY after "
                "the user has explicitly confirmed this action. Any other "
                "value is refused and nothing is executed."),
        }
        required_l.append("confirm")
    return {"type": "object", "properties": properties,
            "required": required_l, "additionalProperties": False}


_ENV_NAME = {
    "type": "string",
    "description": "Environment name (its subdomain label, e.g. 'my-app').",
}
_CONFIRM_NOTE = ("Changes the platform state. Requires confirm=true after "
                 "explicit user confirmation.")


# --- handlers ---------------------------------------------------------------


async def _h_envs_list(client: JelasticClient, args: dict) -> dict:
    envs = await client.list_envs()
    return {"environments": envs, "count": len(envs)}


async def _h_env_info(client: JelasticClient, args: dict) -> dict:
    return {"env": await client.env_info(_req_str(args, "env_name"))}


async def _h_env_stats(client: JelasticClient, args: dict) -> dict:
    env_name = _req_str(args, "env_name")
    return {"env_name": env_name, "nodes": await client.env_stats(env_name)}


async def _h_env_create(client: JelasticClient, args: dict) -> dict:
    env_name = _req_str(args, "env_name")
    stack = _opt_str(args, "stack") or "php:8.3"
    db = _opt_str(args, "db")
    cloudlets = _opt_int(args, "cloudlets")
    nodes = [_stack_node(stack, "cp", cloudlets)]
    if db:
        nodes.append(_stack_node(db, "db", cloudlets))
    result = await client.create_env(
        env_name, nodes=nodes, engine=stack,
        region=_opt_str(args, "region") or "default",
        display_name=_opt_str(args, "display_name"))
    result["detail"] = ("environment creation accepted (asynchronous); poll "
                        "with op_result(env_name, op='status') or env_info")
    return result


async def _h_env_start(client: JelasticClient, args: dict) -> dict:
    return await client.start_env(_req_str(args, "env_name"))


async def _h_env_stop(client: JelasticClient, args: dict) -> dict:
    return await client.stop_env(_req_str(args, "env_name"))


async def _h_env_sleep(client: JelasticClient, args: dict) -> dict:
    return await client.sleep_env(_req_str(args, "env_name"))


async def _h_env_delete(client: JelasticClient, args: dict) -> dict:
    env_name = _req_str(args, "env_name")
    password = args.get("password")
    if not isinstance(password, str) or not password:
        # Reached only when the schema is bypassed; kept defensive.
        raise ToolInputError("argument [password] is required for env_delete")
    result = await client.delete_env(env_name, password, wait=True)
    result["detail"] = "environment deleted and verified gone"
    return result


async def _h_node_scale(client: JelasticClient, args: dict) -> dict:
    return await client.scale_node(
        _req_str(args, "env_name"), _req_int(args, "node_id"),
        _req_int(args, "fixed_cloudlets"), _req_int(args, "flexible_cloudlets"))


async def _h_topology_change(client: JelasticClient, args: dict) -> dict:
    env_name = _req_str(args, "env_name")
    nodes = args.get("nodes")
    if not isinstance(nodes, list) or not nodes or \
            not all(isinstance(n, dict) for n in nodes):
        raise ToolInputError("argument [nodes] must be a non-empty array of "
                             "node specs")
    return await client.change_topology(env_name, nodes)


async def _h_deploy_archive(client: JelasticClient, args: dict) -> dict:
    env_name = _req_str(args, "env_name")
    url = _opt_str(args, "url")
    archive = _opt_str(args, "archive")
    if bool(url) == bool(archive):
        raise ToolInputError("exactly one of [url] or [archive] is required")
    return await client.deploy_archive(
        env_name, context=_opt_str(args, "context") or "ROOT",
        url=url, archive=archive, node_group=_opt_str(args, "node_group"))


async def _h_deploy_jps(client: JelasticClient, args: dict) -> dict:
    return await client.install_jps(
        _req_str(args, "env_name"), _req_str(args, "jps"),
        settings=args.get("settings"), skip_node_emails=True)


async def _h_db_add(client: JelasticClient, args: dict) -> dict:
    env_name = _req_str(args, "env_name")
    engine = _req_str(args, "engine")
    version = _opt_str(args, "version")
    cloudlets = _opt_int(args, "cloudlets")
    info = await client.env_info(env_name)
    existing = info.get("nodes", [])
    if any(n.get("nodeGroup") == "db" for n in existing):
        raise ToolInputError(
            f"environment [{env_name}] already has a database node group")
    # Rebuild current group specs (counts preserved) and append the db group.
    specs = []
    seen = set()
    for node in existing:
        group = node.get("nodeGroup", "cp")
        if group in seen:
            continue
        seen.add(group)
        specs.append({
            "nodeType": node.get("nodeType", group), "nodeGroup": group,
            "count": sum(1 for n in existing if n.get("nodeGroup") == group),
            "fixedCloudlets": node.get("fixedCloudlets", 1),
            "flexibleCloudlets": node.get("flexibleCloudlets", 7),
        })
    db_display = f"{engine}:{version}" if version else engine
    db_spec = {"nodeType": engine, "nodeGroup": "db",
               "displayName": db_display, "count": 1}
    if cloudlets is not None:
        db_spec.update(_cloudlet_split(cloudlets))
    specs.append(db_spec)
    result = await client.change_topology(env_name, specs)
    result["detail"] = (f"database node group [{db_display}] added "
                        "(asynchronous); poll with op_result(env_name, "
                        "op='nodes_ready')")
    return result


async def _h_logs_read(client: JelasticClient, args: dict) -> dict:
    env_name = _req_str(args, "env_name")
    logs = await client.get_logs(env_name, limit=_opt_int(args, "limit"))
    return {"env_name": env_name, "logs": logs, "count": len(logs)}


async def _h_env_var_get(client: JelasticClient, args: dict) -> dict:
    env_name = _req_str(args, "env_name")
    return {"env_name": env_name,
            "vars": await client.get_env_variables(env_name)}


async def _h_env_var_set(client: JelasticClient, args: dict) -> dict:
    env_name = _req_str(args, "env_name")
    variables = args.get("vars")
    if not isinstance(variables, dict) or not variables:
        raise ToolInputError("argument [vars] must be a non-empty object")
    return {"env_name": env_name,
            "vars": await client.set_env_variables(env_name, variables)}


async def _h_ssl_letsencrypt(client: JelasticClient, args: dict) -> dict:
    return await client.bind_ssl(_req_str(args, "env_name"),
                                 _req_str(args, "ext_domain"))


async def _h_marketplace_list(client: JelasticClient, args: dict) -> dict:
    category = _opt_int(args, "category")
    if category is None:
        return {"categories": await client.marketplace_categories()}
    return {"category": category,
            "apps": await client.marketplace_apps(category)}


async def _h_templates_list(client: JelasticClient, args: dict) -> dict:
    templates = await client.list_templates(_opt_str(args, "node_group"))
    return {"templates": templates, "count": len(templates)}


async def _h_quota_get(client: JelasticClient, args: dict) -> dict:
    return {"quotas": await client.get_quotas()}


async def _h_op_result(client: JelasticClient, args: dict) -> dict:
    env_name = _req_str(args, "env_name")
    op = _opt_str(args, "op") or "status"
    timeout = args.get("timeout")
    timeout = float(timeout) if timeout is not None else 30.0
    if timeout <= 0:
        raise ToolInputError("argument [timeout] must be > 0")
    try:
        if op == "status":
            target = args.get("target") or ["running"]
            if not isinstance(target, list) or not all(
                    isinstance(t, str) for t in target):
                raise ToolInputError("argument [target] must be an array of "
                                     "status names")
            observed = await client.wait_env_status(
                env_name, *target, timeout=timeout)
        elif op == "gone":
            await client.wait_env_gone(env_name, timeout=timeout)
            return {"status": "done", "op": op, "env_name": env_name,
                    "detail": "environment no longer exists"}
        elif op == "nodes_ready":
            observed = await client.wait_nodes_ready(env_name, timeout=timeout)
        else:
            raise ToolInputError(
                "argument [op] must be one of: status, gone, nodes_ready")
    except OperationTimeout as exc:
        # Never conclude failure from a timeout: report what was observed.
        return {"status": "in_progress", "op": op, "env_name": env_name,
                "message": str(exc), "observed": exc.observed}
    return {"status": "done", "op": op, "env_name": env_name,
            "observed": observed}


# --- registry ---------------------------------------------------------------

TOOLS: dict[str, ToolSpec] = {spec.name: spec for spec in [
    ToolSpec(
        "envs_list", "List the caller's environments (name, status, domain).",
        _schema({}, mutating=False), _h_envs_list),
    ToolSpec(
        "env_info",
        "Environment details: status, nodes, domains, app, variables, SSL.",
        _schema({"env_name": _ENV_NAME}, ("env_name",), mutating=False),
        _h_env_info),
    ToolSpec(
        "env_stats", "CPU/RAM/disk usage per node of an environment.",
        _schema({"env_name": _ENV_NAME}, ("env_name",), mutating=False),
        _h_env_stats),
    ToolSpec(
        "env_create",
        "Create an environment (wizard-shaped: stack, optional database, "
        "cloudlet preset). " + _CONFIRM_NOTE,
        _schema({
            "env_name": _ENV_NAME,
            "stack": {"type": "string",
                      "description": "App stack, e.g. 'php:8.3', 'java:21', "
                                     "'nodejs:20'. Default 'php:8.3'."},
            "db": {"type": "string",
                   "description": "Optional database node, e.g. 'mysql:8.0', "
                                  "'postgres:16', 'redis:7.2'."},
            "cloudlets": {"type": "integer",
                          "description": "Cloudlets per node (1 fixed + the "
                                         "rest flexible), e.g. 4 (Dev) or 12 "
                                         "(Prod). Default 8."},
            "region": {"type": "string", "description": "Region hint."},
            "display_name": {"type": "string",
                             "description": "Human-readable name."},
        }, ("env_name",), mutating=True), _h_env_create),
    ToolSpec(
        "env_start", "Start a stopped/sleeping environment. " + _CONFIRM_NOTE,
        _schema({"env_name": _ENV_NAME}, ("env_name",), mutating=True),
        _h_env_start),
    ToolSpec(
        "env_stop", "Stop a running environment (keeps it, frees resources). "
        + _CONFIRM_NOTE,
        _schema({"env_name": _ENV_NAME}, ("env_name",), mutating=True),
        _h_env_stop),
    ToolSpec(
        "env_sleep", "Put a running environment to sleep (stopped but "
        "quickly resumable). " + _CONFIRM_NOTE,
        _schema({"env_name": _ENV_NAME}, ("env_name",), mutating=True),
        _h_env_sleep),
    ToolSpec(
        "env_delete",
        "PERMANENTLY delete an environment and all its data. DESTRUCTIVE: "
        "requires confirm=true AND the account password (forwarded once, "
        "never stored).",
        _schema({
            "env_name": _ENV_NAME,
            "password": {"type": "string",
                         "description": "The platform account password. "
                                        "Forwarded once to deleteenv and "
                                        "never stored."},
        }, ("env_name", "password"), mutating=True), _h_env_delete),
    ToolSpec(
        "node_scale",
        "Vertically scale one node (set fixed+flexible cloudlets). "
        + _CONFIRM_NOTE,
        _schema({
            "env_name": _ENV_NAME,
            "node_id": {"type": "integer", "description": "Node id from "
                        "env_info."},
            "fixed_cloudlets": {"type": "integer", "description": "Fixed "
                                "cloudlets (>= 0)."},
            "flexible_cloudlets": {"type": "integer", "description":
                                   "Flexible cloudlets (>= 0)."},
        }, ("env_name", "node_id", "fixed_cloudlets", "flexible_cloudlets"),
            mutating=True), _h_node_scale),
    ToolSpec(
        "topology_change",
        "Change the full topology (horizontal scaling, add/remove node "
        "groups). " + _CONFIRM_NOTE,
        _schema({
            "env_name": _ENV_NAME,
            "nodes": {"type": "array", "items": {"type": "object"}, "description":
                      "Node group specs: {nodeType?, nodeGroup?, count?, "
                      "fixedCloudlets?, flexibleCloudlets?}."},
        }, ("env_name", "nodes"), mutating=True), _h_topology_change),
    ToolSpec(
        "deploy_archive",
        "Deploy an application archive (URL or archive name) to an "
        "environment context. " + _CONFIRM_NOTE,
        _schema({
            "env_name": _ENV_NAME,
            "url": {"type": "string", "description": "Public archive URL."},
            "archive": {"type": "string", "description": "Archive name "
                        "(pre-uploaded). Exactly one of url/archive."},
            "context": {"type": "string", "description": "Deployment context "
                        "path. Default 'ROOT'."},
            "node_group": {"type": "string", "description": "Target node "
                           "group (e.g. 'cp')."},
        }, ("env_name",), mutating=True), _h_deploy_archive),
    ToolSpec(
        "deploy_jps",
        "Install a JPS/marketplace manifest (URL or inline YAML) into an "
        "environment. " + _CONFIRM_NOTE,
        _schema({
            "env_name": _ENV_NAME,
            "jps": {"type": "string", "description": "JPS manifest URL or "
                    "inline YAML/JSON text."},
            "settings": {"description": "Installation settings (object or "
                         "JSON string)."},
        }, ("env_name", "jps"), mutating=True), _h_deploy_jps),
    ToolSpec(
        "db_add",
        "Add a database node group to an existing environment (engine, "
        "version, cloudlets). " + _CONFIRM_NOTE,
        _schema({
            "env_name": _ENV_NAME,
            "engine": {"type": "string", "description": "Database engine: "
                       "'mysql', 'mariadb', 'postgres', 'mongodb', 'redis', "
                       "etc."},
            "version": {"type": "string", "description": "Engine version, "
                        "e.g. '8.0'."},
            "cloudlets": {"type": "integer", "description": "Cloudlets for "
                          "the database node. Default 8."},
        }, ("env_name", "engine"), mutating=True), _h_db_add),
    ToolSpec(
        "logs_read", "Read the recent event/operation log of an environment.",
        _schema({"env_name": _ENV_NAME,
                 "limit": {"type": "integer", "description": "Max entries "
                           "(most recent)."}}, ("env_name",),
                mutating=False), _h_logs_read),
    ToolSpec(
        "env_var_get", "Read an environment's variables.",
        _schema({"env_name": _ENV_NAME}, ("env_name",), mutating=False),
        _h_env_var_get),
    ToolSpec(
        "env_var_set", "Replace an environment's variables. " + _CONFIRM_NOTE,
        _schema({"env_name": _ENV_NAME,
                 "vars": {"type": "object", "description": "Full variable "
                          "map to set (replaces the existing set)."}},
                ("env_name", "vars"), mutating=True), _h_env_var_set),
    ToolSpec(
        "ssl_letsencrypt",
        "Issue a Let's Encrypt SSL certificate for a custom external domain. "
        + _CONFIRM_NOTE,
        _schema({"env_name": _ENV_NAME,
                 "ext_domain": {"type": "string", "description": "External "
                                "domain bound to the environment."}},
                ("env_name", "ext_domain"), mutating=True), _h_ssl_letsencrypt),
    ToolSpec(
        "marketplace_list",
        "Browse the app catalog: categories, or apps in a category.",
        _schema({"category": {"type": "integer", "description": "Category id "
                              "(omit for the category list)."}},
                mutating=False), _h_marketplace_list),
    ToolSpec(
        "templates_list",
        "Available stacks/engines (node types, versions, node groups).",
        _schema({"node_group": {"type": "string", "description": "Filter by "
                               "node group ('cp', 'db', 'balancer')."}},
                mutating=False), _h_templates_list),
    ToolSpec(
        "quota_get", "Account quotas and usage (environments, nodes, "
        "cloudlets).",
        _schema({}, mutating=False), _h_quota_get),
    ToolSpec(
        "op_result",
        "Poll an asynchronous operation (Jelastic ops are fire-and-forget): "
        "wait for an environment status, deletion, or node readiness. Never "
        "concludes failure from a timeout — returns status 'in_progress' "
        "with the last observed state.",
        _schema({
            "env_name": _ENV_NAME,
            "op": {"type": "string", "enum": ["status", "gone", "nodes_ready"],
                   "description": "What to wait for."},
            "target": {"type": "array", "items": {"type": "string"},
                       "description": "For op='status': wanted status names "
                       "(default ['running'])."},
            "timeout": {"type": "number", "description": "Seconds to wait "
                        "(default 30)."},
        }, ("env_name",), mutating=False), _h_op_result),
]}


# --- execution --------------------------------------------------------------


def _friendly_error(exc: JelasticError) -> Tuple[str, str]:
    """Map a typed platform error to (code, human message). Never includes
    credentials."""
    mapping = {
        SessionExpiredError: ("session_expired",
                              "platform session expired or invalid — re-open "
                              "the dashboard to get a fresh session"),
        AuthFailedError: ("auth_failed",
                          "authentication failed — the platform rejected the "
                          "account password"),
        DeleteInProgressError: ("delete_in_progress",
                                "the environment is already being deleted"),
        DomainUnavailableError: ("domain_unavailable",
                                 "environment name/domain not available — "
                                 "choose another name"),
        EnvironmentNotFoundError: ("env_not_found",
                                   "environment not found"),
        NodeNotFoundError: ("node_not_found", "node not found"),
        InvalidParameterError: ("invalid_parameter",
                                "missing or invalid parameter"),
        ManifestError: ("manifest_corrupted",
                        "JPS manifest corrupted / not parsed"),
        OperationFailedError: ("operation_failed",
                               "operation failed on the platform"),
        MalformedResponseError: ("malformed_response",
                                 "unexpected response from the platform "
                                 "(proxy/WAF page?)"),
    }
    for exc_type, (code, message) in mapping.items():
        if isinstance(exc, exc_type):
            if exc.message and code in ("invalid_parameter", "env_not_found",
                                        "node_not_found"):
                message = exc.message
            return code, message
    return "platform_error", exc.message or "platform error"


async def execute_tool(name: str, arguments: Optional[dict],
                       auth: Optional[AuthConfig], *,
                       transport=None, timeout: float = 30.0,
                       poll_interval: float = 0.25) -> Tuple[dict, bool]:
    """Run one tool. Returns ``(payload, is_error)``.

    Safety gates run before anything touches the platform; the password of
    ``env_delete`` is forwarded once and never appears in any payload.
    """
    spec = TOOLS.get(name)
    if spec is None:
        return ({"error": {"code": "unknown_tool",
                           "message": f"unknown tool [{name}]"}}, True)
    args = arguments or {}
    if not isinstance(args, dict):
        return ({"error": {"code": "invalid_parameter",
                           "message": "arguments must be an object"}}, True)

    if auth is None:
        return ({"error": {
            "code": "no_credentials",
            "message": "platform credentials required: X-Jelastic-Host and "
                       "X-Jelastic-Session headers (HTTP) or JELASTIC_HOST / "
                       "JELASTIC_SESSION (stdio)"}}, True)

    if spec.mutating and args.get("confirm") is not True:
        return ({"error": {
            "code": "confirm_required",
            "message": f"[{name}] changes the platform state and was refused: "
                       "re-call with confirm=true ONLY after the user has "
                       "explicitly confirmed this exact action"}}, True)

    if name == "env_delete":
        password = args.get("password")
        if not isinstance(password, str) or not password:
            return ({"error": {
                "code": "password_required",
                "message": "[env_delete] requires the account password "
                           "(argument [password]); it is forwarded once to "
                           "the platform and never stored"}}, True)

    client = JelasticClient(auth.host, auth.session, timeout=timeout,
                            transport=transport, poll_interval=poll_interval)
    try:
        payload = await spec.handler(client, args)
        return payload, False
    except ToolInputError as exc:
        return ({"error": {"code": "invalid_parameter", "message": str(exc)}},
                True)
    except OperationTimeout as exc:
        # A timeout is NOT a failure: the operation may still be running.
        return ({"status": "in_progress", "message": str(exc),
                 "observed": exc.observed}, False)
    except JelasticError as exc:
        code, message = _friendly_error(exc)
        return ({"error": {"code": code, "message": message}}, True)
    finally:
        await client.aclose()
