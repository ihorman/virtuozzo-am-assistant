"""MCP tool surface for Application Management Assistant: specs, safety gates, execution.

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

from . import security
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
    RedirectBlockedError,
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
    """'php:8.3' -> nodeType 'php'; 'docker:nginx:alpine' -> docker node.

    Docker form: ``docker:<image>[:<tag>]`` builds the ``nodeType: docker``
    shape with ``customitem`` dockerName/dockerTag - the docker-centric
    platforms (VHC) require this (verified live).
    """
    stack = stack.strip()
    if not stack:
        raise ToolInputError("empty stack/db spec")
    if stack.startswith("docker:"):
        rest = stack[len("docker:"):].strip()
        if ":" in rest:
            image, tag = rest.rsplit(":", 1)
        else:
            image, tag = rest, "latest"
        image, tag = image.strip(), tag.strip()
        if (not image or not tag or any(c.isspace() for c in image + tag)
                or "/" == image[:1]):
            raise ToolInputError(f"invalid docker spec [{stack}]")
        spec = {"nodeType": "docker", "nodeGroup": node_group,
                "displayName": f"{image}:{tag}", "count": 1,
                # Write shape is 'metadata' (verified live: createenvironment
                # rejects 'customitem', demanding "metadata.dockerName or
                # image"); customitem is only the read shape.
                "metadata": {"dockerName": image, "dockerTag": tag}}
        if cloudlets is not None:
            spec.update(_cloudlet_split(cloudlets))
        return spec
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
    "pattern": r"^[a-z0-9][a-z0-9-]{2,32}$",
    "maxLength": 32,
}

# Anchored validation patterns (docs/SECURITY.md mitigation 4).
NODE_GROUP = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9-]{0,15}$",
              "maxLength": 16}
CONTEXT = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
           "maxLength": 64}
STACK = {"type": "string", "pattern": r"^[a-z0-9][a-z0-9.:+_-]{0,31}$",
         "maxLength": 32}
VERSION = {"type": "string", "pattern": r"^[A-Za-z0-9.]{1,16}$",
           "maxLength": 16}
TEXT = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9 ._:-]{0,63}$",
        "maxLength": 64}
DOMAIN = {"type": "string", "maxLength": 253,
          "pattern": r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                     r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"}
ARCHIVE = {"type": "string", "pattern": r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
           "maxLength": 128}
STATUS = {"type": "string", "pattern": r"^[a-z_]{1,32}$", "maxLength": 32}

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
    rows = await client.env_stats(env_name)
    return {"env_name": env_name,
            "nodes": [_stat_row(row) for row in rows]}


def _stat_row(row: dict) -> dict:
    """Normalize a getsumstat row to the tool contract.

    Live rows carry lowercase ``nodeid`` (string) and ``mem``/``memTotal``
    (KB) instead of the id/ram fields consumers expect - bridge the names
    here so every stand yields the same shape.
    """
    row = dict(row)
    node_id = row.get("id", row.get("nodeId"))
    if node_id is None and row.get("nodeid") is not None:
        try:
            node_id = int(row["nodeid"])
        except (TypeError, ValueError):
            node_id = row["nodeid"]
    if node_id is not None:
        row.setdefault("id", node_id)
        row.setdefault("nodeId", node_id)
    if "ram" not in row:
        mem, total = row.get("mem"), row.get("memTotal")
        if total:
            try:
                row["ram"] = round(float(mem or 0) / float(total) * 100, 1)
            except (TypeError, ValueError):
                pass
        elif mem is not None:
            row["ram"] = mem
    return row


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
        region=_opt_str(args, "region"),
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


def _group_specs(nodes: list) -> dict:
    """Collapse a node list to per-group {count, fixed, flexible} specs."""
    groups: dict = {}
    for node in nodes:
        group = str(node.get("nodeGroup") or node.get("nodeType") or "cp")
        entry = groups.setdefault(group, {"count": 0,
                                          "fixedCloudlets":
                                              node.get("fixedCloudlets"),
                                          "flexibleCloudlets":
                                              node.get("flexibleCloudlets")})
        entry["count"] += 1
    return groups


async def _h_topology_change(client: JelasticClient, args: dict) -> dict:
    """Route by what actually changes (verified live shapes):

    - scaling EXISTING groups -> ``applynodegroupdata`` per group
      (``editnodegroup`` answers 1102 for adds);
    - adding/removing groups -> ``changetopology`` with the full node list
      (the verified add path; the ``env`` descriptor is added by the client).
    """
    env_name = _req_str(args, "env_name")
    nodes = args.get("nodes")
    if not isinstance(nodes, list) or not nodes or \
            not all(isinstance(n, dict) for n in nodes):
        raise ToolInputError("argument [nodes] must be a non-empty array of "
                             "node specs")
    requested = {}
    for spec in nodes:
        group = str(spec.get("nodeGroup") or spec.get("nodeType") or "cp")
        requested[group] = spec
    info = await client.env_info(env_name)
    current = _group_specs(info.get("nodes") or [])
    if requested and set(requested) == set(current):
        changed = {}
        for group, spec in requested.items():
            want_count = spec.get("count")
            changes = {}
            if want_count is not None and int(want_count) != \
                    current[group]["count"]:
                changes["count"] = int(want_count)
            for key in ("fixedCloudlets", "flexibleCloudlets"):
                if spec.get(key) is not None and int(spec[key]) != \
                        current[group].get(key):
                    changes[key] = int(spec[key])
            if changes:
                changed[group] = await client.apply_node_group_data(
                    env_name, group,
                    count=changes.get("count"),
                    fixed_cloudlets=changes.get("fixedCloudlets"),
                    flexible_cloudlets=changes.get("flexibleCloudlets"))
        return {"env_name": env_name, "scaled_groups": sorted(changed),
                "detail": ("node group scaling applied via applynodegroupdata "
                           "(asynchronous); poll with op_result(env_name, "
                           "op='nodes_ready')") if changed else
                          "topology already matches the request"}
    result = await client.change_topology(env_name, nodes)
    result["detail"] = ("topology change accepted (asynchronous); poll with "
                        "op_result(env_name, op='nodes_ready')")
    return result


async def _h_deploy_archive(client: JelasticClient, args: dict) -> dict:
    env_name = _req_str(args, "env_name")
    url = _opt_str(args, "url")
    archive = _opt_str(args, "archive")
    if bool(url) == bool(archive):
        raise ToolInputError("exactly one of [url] or [archive] is required")
    if url:
        security.validate_url(url)  # SSRF guard: refuse private/metadata targets
    return await client.deploy_archive(
        env_name, context=_opt_str(args, "context") or "ROOT",
        url=url, archive=archive, node_group=_opt_str(args, "node_group"))


async def _h_deploy_jps(client: JelasticClient, args: dict) -> dict:
    jps = _req_str(args, "jps")
    if security.looks_like_url(jps):
        security.validate_url(jps)  # SSRF guard on manifest URLs
    result = await client.install_jps(
        _req_str(args, "env_name"), jps,
        settings=args.get("settings"), skip_node_emails=True)
    for field in ("successText", "message", "text"):
        if isinstance(result.get(field), str):
            result[field] = _frame_platform_text(result[field])
    return result


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


def _frame_platform_text(value: Any) -> Any:
    """Frame platform-returned text fields as untrusted data (T4)."""
    if isinstance(value, str):
        return security.frame_untrusted(value)
    return value


def _frame_logs_value(value: Any, key: str = "") -> Any:
    """Recursively frame prose fields of platform log payloads (T4)."""
    if isinstance(value, dict):
        return {k: _frame_logs_value(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_frame_logs_value(v, key) for v in value]
    if isinstance(value, str) and key in ("message", "text", "body"):
        return _frame_platform_text(value)
    return value


async def _h_logs_read(client: JelasticClient, args: dict) -> dict:
    """Three live shapes behind one tool: the log-file listing (``getlogs``),
    one file's content (``readlog`` -> ``{linescount, body}``), or the log
    directory tree (``getlogslist``). All are per-node; ``node_id`` defaults
    to the environment's master node."""
    env_name = _req_str(args, "env_name")
    node_id = _opt_int(args, "node_id")
    path = _opt_str(args, "path")
    if args.get("list_dirs") is True:
        entries = await client.get_logs_list(env_name, node_id=node_id)
        return {"env_name": env_name, "dirs": _frame_logs_value(entries),
                "count": len(entries)}
    if path:
        result = await client.read_log(
            env_name, path, node_id=node_id, line=_opt_int(args, "line"))
        return {"env_name": env_name,
                "path": result["path"],
                "linescount": result["linescount"],
                "body": _frame_logs_value(result.get("body", ""), "body")}
    logs = await client.get_logs(env_name, node_id=node_id,
                                 limit=_opt_int(args, "limit"))
    return {"env_name": env_name, "logs": _frame_logs_value(logs),
            "count": len(logs)}


async def _h_env_var_get(client: JelasticClient, args: dict) -> dict:
    env_name = _req_str(args, "env_name")
    node_group = _opt_str(args, "node_group") or "cp"
    return {"env_name": env_name, "node_group": node_group,
            "vars": await client.get_env_variables(env_name,
                                                   node_group=node_group)}


async def _h_env_var_set(client: JelasticClient, args: dict) -> dict:
    env_name = _req_str(args, "env_name")
    node_group = _opt_str(args, "node_group") or "cp"
    variables = args.get("vars")
    if not isinstance(variables, dict) or not variables:
        raise ToolInputError("argument [vars] must be a non-empty object")
    return {"env_name": env_name, "node_group": node_group,
            "vars": await client.set_env_variables(env_name, variables,
                                                   node_group=node_group)}


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
            "stack": {**STACK,
                      "description": "App stack: 'php:8.3', 'java:21', "
                                     "'nodejs:20', or 'docker:<image>[:<tag>]' "
                                     "(docker-centric platforms), e.g. "
                                     "'docker:nginx:alpine'. Default 'php:8.3'."},
            "db": {**STACK,
                   "description": "Optional database node, e.g. 'mysql:8.0', "
                                  "'postgres:16', 'redis:7.2'."},
            "cloudlets": {"type": "integer", "minimum": 1, "maximum": 128,
                          "description": "Cloudlets per node (1 fixed + the "
                                         "rest flexible), e.g. 4 (Dev) or 12 "
                                         "(Prod). Default 8."},
            "region": {**TEXT, "description": "Region hint."},
            "display_name": {**TEXT,
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
            "password": {"type": "string", "minLength": 1, "maxLength": 256,
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
            "node_id": {"type": "integer", "minimum": 1, "maximum": 10000000,
                        "description": "Node id from env_info."},
            "fixed_cloudlets": {"type": "integer", "minimum": 0,
                                "maximum": 128, "description": "Fixed "
                                "cloudlets (>= 0)."},
            "flexible_cloudlets": {"type": "integer", "minimum": 0,
                                   "maximum": 128, "description":
                                   "Flexible cloudlets (>= 0)."},
        }, ("env_name", "node_id", "fixed_cloudlets", "flexible_cloudlets"),
            mutating=True), _h_node_scale),
    ToolSpec(
        "topology_change",
        "Change the full topology (horizontal scaling, add/remove node "
        "groups). " + _CONFIRM_NOTE,
        _schema({
            "env_name": _ENV_NAME,
            "nodes": {"type": "array", "maxItems": 20,
                      "items": {"type": "object",
                                "allowedKeys": ["nodeType", "nodeGroup",
                                                "count", "fixedCloudlets",
                                                "flexibleCloudlets",
                                                "displayName"],
                                "valueMaxLength": 64},
                      "description":
                      "Node group specs: {nodeType?, nodeGroup?, count?, "
                      "fixedCloudlets?, flexibleCloudlets?}."},
        }, ("env_name", "nodes"), mutating=True), _h_topology_change),
    ToolSpec(
        "deploy_archive",
        "Deploy an application archive (URL or archive name) to an "
        "environment context. " + _CONFIRM_NOTE,
        _schema({
            "env_name": _ENV_NAME,
            "url": {"type": "string", "maxLength": 2048,
                    "description": "Public archive URL (SSRF-guarded: only "
                                   "public http(s) targets are accepted)."},
            "archive": {**ARCHIVE, "description": "Archive name "
                        "(pre-uploaded). Exactly one of url/archive."},
            "context": {**CONTEXT, "description": "Deployment context "
                        "path. Default 'ROOT'."},
            "node_group": {**NODE_GROUP, "description": "Target node "
                           "group (e.g. 'cp')."},
        }, ("env_name",), mutating=True), _h_deploy_archive),
    ToolSpec(
        "deploy_jps",
        "Install a JPS/marketplace manifest (URL or inline YAML) into an "
        "environment. " + _CONFIRM_NOTE,
        _schema({
            "env_name": _ENV_NAME,
            "jps": {"type": "string", "maxLength": 65536,
                    "description": "JPS manifest URL (SSRF-guarded) or "
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
            "engine": {**STACK, "description": "Database engine: "
                       "'mysql', 'mariadb', 'postgres', 'mongodb', 'redis', "
                       "'etc.'"},
            "version": {**VERSION, "description": "Engine version, "
                        "e.g. '8.0'."},
            "cloudlets": {"type": "integer", "minimum": 1, "maximum": 128,
                          "description": "Cloudlets for "
                          "the database node. Default 8."},
        }, ("env_name", "engine"), mutating=True), _h_db_add),
    ToolSpec(
        "logs_read",
        "Read a node's logs: the log-file listing (default), one file's "
        "content (path=...), or the log directory tree (list_dirs=true). "
        "node_id defaults to the environment's master node.",
        _schema({"env_name": _ENV_NAME,
                 "node_id": {"type": "integer", "minimum": 1,
                             "maximum": 10000000, "description": "Node id "
                             "from env_info (default: master node)."},
                 "path": {"type": "string", "maxLength": 256,
                          "pattern": r"^/[A-Za-z0-9._/+-]+$",
                          "description": "Log file path from the listing, "
                                         "e.g. '/var/log/cron' — reads its "
                                         "content (readlog)."},
                 "line": {"type": "integer", "minimum": 1, "maximum": 5000,
                          "description": "How many lines to read from the "
                                         "file head (default: whole file)."},
                 "list_dirs": {"type": "boolean",
                               "description": "Return the log directory "
                                              "tree (getlogslist) instead "
                                              "of the file listing."},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 500,
                           "description": "Max listing entries."}},
                ("env_name",), mutating=False), _h_logs_read),
    ToolSpec(
        "env_var_get", "Read a node group's container environment variables.",
        _schema({"env_name": _ENV_NAME,
                 "node_group": {**NODE_GROUP,
                                "description": "Node group to read "
                                               "(default 'cp')."}},
                ("env_name",), mutating=False), _h_env_var_get),
    ToolSpec(
        "env_var_set",
        "Replace a node group's container environment variables. "
        + _CONFIRM_NOTE,
        _schema({"env_name": _ENV_NAME,
                 "node_group": {**NODE_GROUP,
                                "description": "Node group to write "
                                               "(default 'cp')."},
                 "vars": {"type": "object", "maxProperties": 100,
                          "keyPattern": r"^[A-Za-z_][A-Za-z0-9_]{0,63}$",
                          "valueMaxLength": 4096,
                          "description": "Full variable "
                          "map to set (replaces the existing set)."}},
                ("env_name", "vars"), mutating=True), _h_env_var_set),
    ToolSpec(
        "ssl_letsencrypt",
        "Issue a Let's Encrypt SSL certificate for a custom external domain. "
        + _CONFIRM_NOTE,
        _schema({"env_name": _ENV_NAME,
                 "ext_domain": {**DOMAIN, "description": "External "
                                "domain bound to the environment."}},
                ("env_name", "ext_domain"), mutating=True), _h_ssl_letsencrypt),
    ToolSpec(
        "marketplace_list",
        "Browse the app catalog: categories, or apps in a category.",
        _schema({"category": {"type": "integer", "minimum": 0, "maximum": 9999,
                              "description": "Category id "
                              "(omit for the category list)."}},
                mutating=False), _h_marketplace_list),
    ToolSpec(
        "templates_list",
        "Available stacks/engines (node types, versions, node groups).",
        _schema({"node_group": {**NODE_GROUP, "description": "Filter by "
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
            "target": {"type": "array", "items": STATUS, "maxItems": 10,
                       "description": "For op='status': wanted status names "
                       "(default ['running'])."},
            "timeout": {"type": "number", "minimum": 0.001, "maximum": 600,
                        "description": "Seconds to wait "
                        "(default 30, capped at 600)."},
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
        RedirectBlockedError: ("redirect_refused",
                               "the platform answered with a redirect; "
                               "refused so the session token cannot leave "
                               "the platform origin"),
    }
    for exc_type, (code, message) in mapping.items():
        if isinstance(exc, exc_type):
            if exc.message and code in ("invalid_parameter", "env_not_found",
                                        "node_not_found"):
                message = exc.message
            return code, message
    return "platform_error", exc.message or "platform error"


# Default policy for direct execute_tool calls (the server passes its own).
_DEFAULT_POLICY = security.SecurityPolicy.from_env()


async def execute_tool(name: str, arguments: Optional[dict],
                       auth: Optional[AuthConfig], *,
                       transport=None, timeout: float = 30.0,
                       poll_interval: float = 0.25,
                       policy: Optional[security.SecurityPolicy] = None,
                       audit: Optional[security.AuditLog] = None) -> Tuple[dict, bool]:
    """Run one tool. Returns ``(payload, is_error)``.

    Security gates run before anything touches the platform (docs/SECURITY.md):
    host allowlist (token pinning), read-only mode, the ``confirm`` gate,
    strict input validation, then per-session rate limits. Every result is
    secret-masked; the ``env_delete`` password is forwarded once and never
    appears in any payload or audit entry.
    """
    policy = policy or _DEFAULT_POLICY

    def _fail(code: str, message: str) -> Tuple[dict, bool]:
        payload = {"error": {"code": code, "message": message}}
        if audit is not None:
            audit.record({"tool": name, "ok": False, "code": code,
                          "args": args})
        return security.mask_secrets(payload), True

    spec = TOOLS.get(name)
    args = arguments or {}
    if spec is None:
        return _fail("unknown_tool", f"unknown tool [{name}]")
    if not isinstance(args, dict):
        return _fail("invalid_parameter", "arguments must be an object")

    if auth is None:
        return _fail("no_credentials",
                     "platform credentials required: X-Jelastic-Host and "
                     "X-Jelastic-Session headers (HTTP) or JELASTIC_HOST / "
                     "JELASTIC_SESSION (stdio)")

    try:
        policy.check_host(auth.host)
    except security.HostNotAllowedError as exc:
        return _fail(exc.code, str(exc))

    if spec.mutating and policy.readonly_now():
        return _fail("read_only_mode",
                     f"[{name}] was refused: the server is in read-only mode "
                     f"(VAM_MCP_READONLY=1); mutating tools are disabled")

    if spec.mutating and args.get("confirm") is not True:
        return _fail("confirm_required",
                     f"[{name}] changes the platform state and was refused: "
                     f"re-call with confirm=true ONLY after the user has "
                     f"explicitly confirmed this exact action")

    if name == "env_delete":
        password = args.get("password")
        if not isinstance(password, str) or not password:
            return _fail("password_required",
                         "[env_delete] requires the account password "
                         "(argument [password]); it is forwarded once to "
                         "the platform and never stored")

    try:
        security.validate_args(spec.input_schema, args, name)
    except security.ParamRejected as exc:
        return _fail(exc.code, str(exc))

    try:
        policy.limiter.check(
            policy.limiter.session_key(auth.session), mutating=spec.mutating)
    except security.RateLimitExceeded as exc:
        return _fail(exc.code, str(exc))

    client = JelasticClient(auth.host, auth.session, timeout=timeout,
                            transport=transport, poll_interval=poll_interval)
    try:
        payload = await spec.handler(client, args)
        is_error = False
    except ToolInputError as exc:
        payload, is_error = ({"error": {"code": "invalid_parameter",
                                        "message": str(exc)}}, True)
    except security.SecurityViolation as exc:
        payload, is_error = ({"error": {"code": exc.code,
                                        "message": str(exc)}}, True)
    except OperationTimeout as exc:
        # A timeout is NOT a failure: the operation may still be running.
        payload, is_error = ({"status": "in_progress", "message": str(exc),
                              "observed": exc.observed}, False)
    except JelasticError as exc:
        code, message = _friendly_error(exc)
        payload, is_error = ({"error": {"code": code, "message": message}},
                             True)
    finally:
        await client.aclose()

    payload = security.mask_secrets(payload)
    if audit is not None:
        audit.record({"tool": name, "mutating": spec.mutating,
                      "ok": not is_error, "args": args})
    return payload, is_error
