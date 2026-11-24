"""In-memory state machine for the mock Jelastic platform.

No HTTP here: every method takes plain params and returns a
``(result_code, payload)`` tuple; ``mock.server`` wraps that into the real
envelope ``{"result": <code>, ...}`` / ``{"result": <code>, "error": "..."}``.

Async semantics are modeled with lazy, timestamp-based transitions: an
operation schedules a pending target state at ``now + transition_seconds``;
the transition is applied on the next state read (or explicit ``advance()``).
This gives fire-and-forget behavior with observable status transitions and
needs no background timers, so it is deterministic under test.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .codes import (
    AUTH_FAILED,
    CONFIRMATION_FAILED,
    DELETE_IN_PROGRESS,
    DOMAIN_UNAVAILABLE,
    ENV_NOT_FOUND,
    INVALID_PARAMETER,
    MANIFEST_CORRUPTED,
    NODE_NOT_FOUND,
    OK,
    SESSION_INVALID,
    STATUS_CODES,
    TRANSIENT_STATES,
)

ENV_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,29}$")
DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")

# --- static catalog data (marketplace / templates) -------------------------

MARKETPLACE_CATEGORIES = [
    {"id": 1, "name": "Applications"},
    {"id": 2, "name": "Databases"},
    {"id": 3, "name": "Docker & Containers"},
    {"id": 4, "name": "Add-ons"},
]

MARKETPLACE_APPS = [
    {"id": 14, "name": "WordPress", "category": 1,
     "description": "Single-instance WordPress with MySQL"},
    {"id": 15, "name": "WordPress Cluster", "category": 1,
     "description": "Highly-available WordPress cluster"},
    {"id": 16, "name": "Nextcloud", "category": 1,
     "description": "Self-hosted file sync and collaboration"},
    {"id": 17, "name": "Magento", "category": 1,
     "description": "E-commerce platform with MySQL and Redis"},
    {"id": 21, "name": "MySQL", "category": 2,
     "description": "MySQL database node"},
    {"id": 22, "name": "MariaDB", "category": 2,
     "description": "MariaDB database node"},
    {"id": 23, "name": "PostgreSQL", "category": 2,
     "description": "PostgreSQL database node"},
    {"id": 24, "name": "Redis", "category": 2,
     "description": "Redis in-memory data store"},
    {"id": 31, "name": "Docker CE", "category": 3,
     "description": "Docker Engine on a standalone container"},
    {"id": 41, "name": "Let's Encrypt SSL", "category": 4,
     "description": "Free SSL certificates for custom domains"},
    {"id": 42, "name": "NFS Storage", "category": 4,
     "description": "Shared NFS storage node"},
]

TEMPLATES = [
    {"nodeType": "php", "name": "PHP", "versions": ["8.3", "8.2", "8.1"],
     "nodeGroups": ["cp"]},
    {"nodeType": "java", "name": "Java", "versions": ["21", "17", "11"],
     "nodeGroups": ["cp"]},
    {"nodeType": "nodejs", "name": "Node.js", "versions": ["20", "18"],
     "nodeGroups": ["cp"]},
    {"nodeType": "golang", "name": "Go", "versions": ["1.22", "1.21"],
     "nodeGroups": ["cp"]},
    {"nodeType": "ruby", "name": "Ruby", "versions": ["3.3", "3.2"],
     "nodeGroups": ["cp"]},
    {"nodeType": "python", "name": "Python", "versions": ["3.12", "3.11"],
     "nodeGroups": ["cp"]},
    {"nodeType": "mysql", "name": "MySQL", "versions": ["8.0", "5.7"],
     "nodeGroups": ["db"]},
    {"nodeType": "mariadb", "name": "MariaDB", "versions": ["10.11", "10.6"],
     "nodeGroups": ["db"]},
    {"nodeType": "postgres", "name": "PostgreSQL", "versions": ["16", "15"],
     "nodeGroups": ["db"]},
    {"nodeType": "mongodb", "name": "MongoDB", "versions": ["7.0", "6.0"],
     "nodeGroups": ["db"]},
    {"nodeType": "redis", "name": "Redis", "versions": ["7.2", "7.0"],
     "nodeGroups": ["db", "cache"]},
    {"nodeType": "nginx", "name": "NGINX", "versions": ["1.25"],
     "nodeGroups": ["balancer"]},
]


@dataclass
class MockConfig:
    """Knobs for the mock platform (tests shrink the timings)."""

    login: str = "demo"
    password: str = "demo"
    session_ttl: float = 1800.0
    transition_seconds: float = 0.5
    domain_suffix: str = "mock.jelastic.local"
    clock: Callable[[], float] = time.monotonic


@dataclass
class Node:
    id: int
    node_type: str
    node_group: str
    display_name: str = ""
    fixed_cloudlets: int = 1
    flexible_cloudlets: int = 7
    status: str = "ok"  # "ok" | "updating" (async topology/scaling work)
    pending: Optional[tuple[float, str]] = None
    stats: dict = field(
        default_factory=lambda: {"cpu": 12.5, "ram": 34.0, "disk": 21.0}
    )

    def advance(self, now: float) -> None:
        if self.pending and now >= self.pending[0]:
            self.status = self.pending[1]
            self.pending = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "nodeType": self.node_type,
            "nodeGroup": self.node_group,
            "displayName": self.display_name or self.node_type,
            "status": self.status,
            "fixedCloudlets": self.fixed_cloudlets,
            "flexibleCloudlets": self.flexible_cloudlets,
        }


@dataclass
class Env:
    name: str
    domain: str
    region: str
    engine: Optional[str]
    display_name: str
    owner: str
    created: float
    status: str = "creating"
    # (ready_at, target_status); target "__remove__" deletes the env.
    pending: Optional[tuple[float, str]] = None
    nodes: list = field(default_factory=list)
    env_vars: dict = field(default_factory=dict)
    logs: list = field(default_factory=list)
    app: Optional[dict] = None
    installations: list = field(default_factory=list)
    ssl: Optional[dict] = None

    def advance(self, now: float) -> None:
        if self.pending and now >= self.pending[0]:
            target = self.pending[1]
            self.pending = None
            if target == "__remove__":
                self.status = "__removed__"
            else:
                self.status = target
        for node in self.nodes:
            node.advance(now)

    @property
    def removed(self) -> bool:
        return self.status == "__removed__"

    def to_summary(self) -> dict:
        # Live shape (VHC 8.15.1): numeric status only - there is NO
        # statusName field; the client synthesizes one.
        return {
            "envName": self.name,
            "displayName": self.display_name or self.name,
            "domain": self.domain,
            "status": STATUS_CODES.get(self.status, -1),
            "region": self.region,
            "engine": self.engine,
            "created": self.created,
        }

    def to_env(self) -> dict:
        """The env object incl. mock extension fields (vars/app/installs/ssl)."""
        env = self.to_summary()
        env["envVars"] = dict(self.env_vars)
        env["app"] = dict(self.app) if self.app else None
        env["installations"] = [dict(i) for i in self.installations]
        env["ssl"] = dict(self.ssl) if self.ssl else None
        return env

    def to_node_groups(self) -> list:
        groups = []
        seen = set()
        for node in self.nodes:
            if node.node_group not in seen:
                seen.add(node.node_group)
                groups.append({"name": node.node_group,
                               "displayName": node.node_group,
                               "templateType": "NATIVE",
                               "scalingMode": "STATELESS"})
        return groups


class MockJelastic:
    """The mock platform: accounts, sessions, environments."""

    def __init__(self, config: Optional[MockConfig] = None):
        self.config = config or MockConfig()
        self.sessions: dict[str, float] = {}  # token -> expiry (clock)
        self.envs: dict[str, Env] = {}
        self._next_node_id = 1001
        #: REST method names seen by the server (for routing assertions).
        self.calls: list = []

    # -- helpers ------------------------------------------------------------

    def _now(self) -> float:
        return self.config.clock()

    def advance_all(self) -> None:
        """Apply any due transitions (also happens lazily on every call)."""
        now = self._now()
        for env in list(self.envs.values()):
            env.advance(now)
            if env.removed:
                del self.envs[env.name]

    def expire_sessions(self) -> None:
        """Test helper: force every session to be invalid."""
        self.sessions.clear()

    def _auth(self, session: Optional[str]) -> int:
        if not session:
            return SESSION_INVALID
        expiry = self.sessions.get(session)
        if expiry is None:
            return SESSION_INVALID
        if self._now() >= expiry:
            del self.sessions[session]
            return SESSION_INVALID
        return OK

    def _get_env(self, env_name: Optional[str]) -> tuple[int, Optional[Env]]:
        self.advance_all()
        env = self.envs.get(env_name or "")
        if env is None:
            return ENV_NOT_FOUND, None
        return OK, env

    def _new_node_id(self) -> int:
        self._next_node_id += 1
        return self._next_node_id

    def _log(self, env: Env, message: str) -> None:
        env.logs.append(f"[{self._now():.3f}] {message}")

    def _make_nodes(self, specs: list) -> list:
        nodes = []
        for spec in specs:
            count = int(spec.get("count", 1) or 1)
            for _ in range(count):
                nodes.append(
                    Node(
                        id=self._new_node_id(),
                        node_type=str(spec.get("nodeType", "cp")),
                        node_group=str(spec.get("nodeGroup", spec.get("nodeType", "cp"))),
                        display_name=str(spec.get("displayName", "")),
                        fixed_cloudlets=int(spec.get("fixedCloudlets", 1) or 1),
                        flexible_cloudlets=int(spec.get("flexibleCloudlets", 7) or 7),
                    )
                )
        return nodes

    # -- auth ---------------------------------------------------------------

    def signin(self, login: Optional[str], password: Optional[str]):
        if login != self.config.login or password != self.config.password:
            return AUTH_FAILED, {"error": "authentication failed"}
        token = "mock-sess-" + secrets.token_hex(16)
        self.sessions[token] = self._now() + self.config.session_ttl
        return OK, {"session": token}

    def signout(self, session: Optional[str]):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        if session:
            self.sessions.pop(session, None)
        return OK, {}

    # -- environments -------------------------------------------------------

    def getenvs(self, session: Optional[str]):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        self.advance_all()
        return OK, {"infos": [e.to_summary() for e in self.envs.values()]}

    def getenvinfo(self, session: Optional[str], env_name: Optional[str]):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        # Live shape (VHC 8.15.1): env/nodes/nodeGroups flat at top level.
        return OK, {"env": env.to_env(),
                    "nodes": [n.to_dict() for n in env.nodes],
                    "nodeGroups": env.to_node_groups()}

    def getsumstat(self, session: Optional[str], env_name: Optional[str]):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        # Live shape: one stats row per node plus an aggregate "sum" row
        # (lowercase "nodeid"), and appid/status on the envelope.
        stats = []
        for n in env.nodes:
            stats.append({
                "nodeid": str(n.id), "nodeGroup": n.node_group,
                "cpu": n.stats["cpu"], "mem": n.stats["ram"],
                "memTotal": 100.0, "disk": n.stats["disk"],
                "capacity": n.fixed_cloudlets + n.flexible_cloudlets,
                "cloudletsused": n.fixed_cloudlets + n.flexible_cloudlets,
                "net": {"in_ext": 0, "in_int": 0, "out_int": 0, "out_ext": 0},
                "vType": "CT",
            })
        if stats:
            stats.append({
                "nodeid": "sum",
                "cpu": sum(s["cpu"] for s in stats) / len(stats),
                "mem": sum(s["mem"] for s in stats),
                "memTotal": sum(s["memTotal"] for s in stats),
                "disk": sum(s["disk"] for s in stats),
                "capacity": sum(s["capacity"] for s in stats),
            })
        return OK, {"stats": stats, "appid": f"mock-{env.name}",
                    "status": STATUS_CODES.get(env.status, -1)}

    def createenvironment(self, session: Optional[str], env: Optional[dict],
                          nodes: Optional[list]):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        env = env or {}
        name = env.get("shortdomain") or env.get("envName")
        if not name or not ENV_NAME_RE.match(str(name)):
            return INVALID_PARAMETER, {
                "error": "invalid parameter [shortdomain] for method [CreateEnv]"
            }
        name = str(name)
        self.advance_all()
        if name in self.envs:
            return DOMAIN_UNAVAILABLE, {"error": "domain not available"}
        now = self._now()
        created = Env(
            name=name,
            domain=f"{name}.{self.config.domain_suffix}",
            region=str(env.get("region", "default")),
            engine=env.get("engine"),
            display_name=str(env.get("displayName", "")),
            owner=self.config.login,
            created=now,
            status="creating",
            pending=(now + self.config.transition_seconds, "running"),
            nodes=self._make_nodes(nodes or [
                {"nodeType": "cp", "nodeGroup": "cp",
                 "fixedCloudlets": 1, "flexibleCloudlets": 7}
            ]),
        )
        for node in created.nodes:
            node.status = "updating"
            node.pending = (now + self.config.transition_seconds, "ok")
        self._log(created, f"environment {name} creation requested")
        self.envs[name] = created
        return OK, {"envName": name, "domain": created.domain,
                    "statusName": created.status}

    # -- lifecycle (fire-and-forget with async transitions) ------------------

    def _lifecycle(self, session: Optional[str], env_name: Optional[str],
                   op: str, from_states: tuple, transient: str, target: str):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        if env.status == "deleting" or env.removed:
            return DELETE_IN_PROGRESS, {
                "error": f"environment [{env_name}] is being deleted"
            }
        if env.status == target:
            return OK, {"envName": env.name, "statusName": env.status}  # idempotent
        if env.pending:
            if env.pending[1] == target:
                return OK, {"envName": env.name, "statusName": env.status}
            return INVALID_PARAMETER, {
                "error": f"another operation is in progress for [{env_name}]"
            }
        if env.status not in from_states:
            return INVALID_PARAMETER, {
                "error": f"method [{op}] is not allowed in status [{env.status}]"
            }
        now = self._now()
        env.status = transient
        env.pending = (now + self.config.transition_seconds, target)
        self._log(env, f"{op} requested")
        return OK, {"envName": env.name, "statusName": env.status}

    def startenv(self, session, env_name):
        return self._lifecycle(session, env_name, "StartEnv",
                               ("stopped", "sleep"), "starting", "running")

    def stopenv(self, session, env_name):
        return self._lifecycle(session, env_name, "StopEnv",
                               ("running", "sleep"), "stopping", "stopped")

    def sleepenv(self, session, env_name):
        return self._lifecycle(session, env_name, "SleepEnv",
                               ("running",), "sleeping", "sleep")

    def deleteenv(self, session, env_name, password, passphrase=None):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        # Verified live: deleteenv needs the typed confirmation
        # passphrase=<envName> - result 724 "Confirmation failed" without it.
        if passphrase != env_name:
            return CONFIRMATION_FAILED, {"error": "Confirmation failed"}
        # The real API rejects a missing password parameter before anything else.
        if password is None or password == "":
            return INVALID_PARAMETER, {
                "error": "invalid parameter [password] for method [DeleteEnv]"
            }
        if password != self.config.password:
            return AUTH_FAILED, {"error": "authentication failed"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        if env.status == "deleting" or env.removed:
            # 2212 semantics: a delete retry while deletion is running.
            return DELETE_IN_PROGRESS, {
                "error": f"environment [{env_name}] is being deleted"
            }
        now = self._now()
        env.status = "deleting"
        env.pending = (now + self.config.transition_seconds, "__remove__")
        self._log(env, "delete requested")
        return OK, {"envName": env.name, "statusName": env.status}

    # -- topology & scaling --------------------------------------------------

    def changetopology(self, session, env_name, nodes, env=None):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        # Verified live: changetopology requires the env descriptor
        # {"region": "", "sslstate": false} - "invalid parameter [env]" without.
        if not isinstance(env, dict):
            return INVALID_PARAMETER, {
                "error": "invalid parameter [env] for method [ChangeTopology]"
            }
        code, env_obj = self._get_env(env_name)
        if env_obj is None:
            return code, {"error": f"environment [{env_name}] not found"}
        env = env_obj
        if env.status == "deleting" or env.removed:
            return DELETE_IN_PROGRESS, {
                "error": f"environment [{env_name}] is being deleted"
            }
        if env.status in TRANSIENT_STATES:
            return INVALID_PARAMETER, {
                "error": f"another operation is in progress for [{env_name}]"
            }
        if not nodes:
            return INVALID_PARAMETER, {
                "error": "invalid parameter [nodes] for method [ChangeTopology]"
            }
        now = self._now()
        ready_at = now + self.config.transition_seconds
        for spec in nodes:
            group = str(spec.get("nodeGroup", spec.get("nodeType", "cp")))
            count = int(spec.get("count", 1) or 1)
            in_group = [n for n in env.nodes if n.node_group == group]
            while len(in_group) > count:
                env.nodes.remove(in_group.pop())
            while len(in_group) < count:
                node = Node(
                    id=self._new_node_id(),
                    node_type=str(spec.get("nodeType", group)),
                    node_group=group,
                    display_name=str(spec.get("displayName", "")),
                    fixed_cloudlets=int(spec.get("fixedCloudlets", 1) or 1),
                    flexible_cloudlets=int(spec.get("flexibleCloudlets", 7) or 7),
                    status="updating",
                    pending=(ready_at, "ok"),
                )
                env.nodes.append(node)
                in_group.append(node)
            for node in in_group:
                node.fixed_cloudlets = int(spec.get("fixedCloudlets",
                                                   node.fixed_cloudlets) or 1)
                node.flexible_cloudlets = int(spec.get("flexibleCloudlets",
                                                       node.flexible_cloudlets) or 7)
                node.status = "updating"
                node.pending = (ready_at, "ok")
        self._log(env, f"topology change requested ({len(nodes)} group spec(s))")
        return OK, {"envName": env.name, "nodes": [n.to_dict() for n in env.nodes]}

    def setcloudletscountbyid(self, session, env_name, node_id,
                              fixed_cloudlets, flexible_cloudlets):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        if env.status == "deleting" or env.removed:
            return DELETE_IN_PROGRESS, {
                "error": f"environment [{env_name}] is being deleted"
            }
        try:
            node_id = int(node_id)
            fixed = int(fixed_cloudlets)
            flexible = int(flexible_cloudlets)
        except (TypeError, ValueError):
            return INVALID_PARAMETER, {
                "error": "invalid parameter [cloudlets] for method [SetCloudletsCountById]"
            }
        node = next((n for n in env.nodes if n.id == node_id), None)
        if node is None:
            return NODE_NOT_FOUND, {"error": f"node [{node_id}] not found"}
        now = self._now()
        node.fixed_cloudlets = fixed
        node.flexible_cloudlets = flexible
        node.status = "updating"
        node.pending = (now + self.config.transition_seconds, "ok")
        self._log(env, f"node {node_id} cloudlets set to {fixed}/{flexible}")
        return OK, {"envName": env.name, "node": node.to_dict()}

    def applynodegroupdata(self, session, env_name, node_group, data=None):
        # Verified live shape: nodeGroup + data={"count","fixedCloudlets",
        # "flexibleCloudlets"}. Existing groups only (editnodegroup answers
        # 1102 for adds - use changetopology to add nodes).
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        if env.status == "deleting" or env.removed:
            return DELETE_IN_PROGRESS, {
                "error": f"environment [{env_name}] is being deleted"
            }
        if not isinstance(data, dict):
            return INVALID_PARAMETER, {
                "error": "invalid parameter [data] for method [ApplyNodeGroupData]"
            }
        group = str(node_group or "")
        in_group = [n for n in env.nodes if n.node_group == group]
        if not in_group:
            return INVALID_PARAMETER, {
                "error": f"invalid parameter [nodeGroup] for method "
                         f"[ApplyNodeGroupData]"
            }
        try:
            count = int(data.get("count", len(in_group)) or len(in_group))
        except (TypeError, ValueError):
            return INVALID_PARAMETER, {
                "error": "invalid parameter [count] for method "
                         "[ApplyNodeGroupData]"
            }
        now = self._now()
        ready_at = now + self.config.transition_seconds
        while len(in_group) > count:
            env.nodes.remove(in_group.pop())
        while len(in_group) < count:
            node = Node(
                id=self._new_node_id(),
                node_type=in_group[0].node_type,
                node_group=group,
                display_name=in_group[0].display_name,
                fixed_cloudlets=int(data.get("fixedCloudlets",
                                             in_group[0].fixed_cloudlets) or 1),
                flexible_cloudlets=int(data.get("flexibleCloudlets",
                                                in_group[0].flexible_cloudlets)
                                       or 7),
                status="updating",
                pending=(ready_at, "ok"),
            )
            env.nodes.append(node)
            in_group.append(node)
        for node in in_group:
            node.fixed_cloudlets = int(data.get("fixedCloudlets",
                                                node.fixed_cloudlets) or 1)
            node.flexible_cloudlets = int(data.get("flexibleCloudlets",
                                                   node.flexible_cloudlets) or 7)
            node.status = "updating"
            node.pending = (ready_at, "ok")
        self._log(env, f"node group {group} scaled to count={count}")
        return OK, {"envName": env.name, "nodeGroup": group,
                    "nodes": [n.to_dict() for n in in_group]}

    # -- deployment ----------------------------------------------------------

    def deployarchive(self, session, env_name, context=None, fileUrl=None,
                      fileName=None, node_group=None, **_ignored):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        if env.status == "deleting" or env.removed:
            return DELETE_IN_PROGRESS, {
                "error": f"environment [{env_name}] is being deleted"
            }
        # Verified live shape: fileUrl + fileName (url/archive are rejected).
        if not fileUrl and not fileName:
            return INVALID_PARAMETER, {
                "error": "invalid parameter [fileUrl] for method [DeployApp]"
            }
        if env.status != "running":
            return INVALID_PARAMETER, {
                "error": f"environment [{env_name}] is not running"
            }
        context = context or "ROOT"
        env.app = {
            "context": context,
            "fileUrl": fileUrl,
            "fileName": fileName,
            "nodeGroup": node_group,
            "deployedAt": self._now(),
        }
        self._log(env, f"deployed {fileUrl or fileName} to context {context}")
        return OK, {"domain": env.domain, "context": context}

    def install_jps(self, session, jps, env_name=None, settings=None,
                    skip_node_emails=None):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        if not jps or not str(jps).strip():
            return INVALID_PARAMETER, {
                "error": "invalid parameter [jps] for method [Install]"
            }
        jps = str(jps)
        # Faithful pitfall: non-ASCII or tab indentation breaks manifest parsing.
        if not jps.startswith(("http://", "https://")):
            if any(ord(c) > 127 for c in jps) or "\t" in jps:
                return MANIFEST_CORRUPTED, {
                    "error": "JPS manifest corrupted / not parsed"
                }
        if settings is not None:
            import json

            try:
                json.loads(settings) if isinstance(settings, str) else settings
            except ValueError:
                return INVALID_PARAMETER, {
                    "error": "invalid parameter [settings] for method [Install]"
                }
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        if env.status == "deleting" or env.removed:
            return DELETE_IN_PROGRESS, {
                "error": f"environment [{env_name}] is being deleted"
            }
        record = {
            "id": len(env.installations) + 1,
            "jps": jps if jps.startswith(("http://", "https://")) else "<inline>",
            "settings": settings,
            "skipNodeEmails": bool(skip_node_emails),
        }
        env.installations.append(record)
        self._log(env, f"JPS install #{record['id']} completed")
        return OK, {
            "successText": "Application installed successfully",
            "response": {"domain": env.domain, "responses": []},
        }

    # -- logs (live shapes: getlogs listing, readlog content, getlogslist) ---

    def _log_files(self, env: "Env") -> list:
        return [
            {"path": f"/var/log/{env.name}.log", "name": f"{env.name}.log",
             "size": sum(len(line) for line in env.logs), "isExported": False,
             "permission": "rwd", "lastModified": "", "isdir": False,
             "fileType": "-"},
            {"path": "/var/log/cron", "name": "cron", "size": 200,
             "isExported": False, "permission": "rwd", "lastModified": "",
             "isdir": False, "fileType": "-"},
        ]

    def _log_lines(self, env: "Env", path: str) -> Optional[list]:
        if path == f"/var/log/{env.name}.log":
            return list(env.logs)
        if path == "/var/log/cron":
            return ["(CRON) STARTUP (1.5.7)",
                    "(CRON) INFO (running with inotify support)"]
        return None

    def _node_for_logs(self, env: "Env", node_id):
        if node_id in (None, ""):
            return None, (INVALID_PARAMETER, {
                "error": "Parameter 'nodeid' is required."})
        try:
            node_id = int(node_id)
        except (TypeError, ValueError):
            return None, (INVALID_PARAMETER, {
                "error": "Parameter 'nodeid' is required."})
        node = next((n for n in env.nodes if n.id == node_id), None)
        if node is None:
            return None, (NODE_NOT_FOUND, {"error": f"node [{node_id}] not found"})
        return node, None

    def getlogs(self, session, env_name, node_id=None, **_ignored):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        _, error = self._node_for_logs(env, node_id)
        if error:
            return error
        # Live shape: keyword groups, each with its file entries.
        files = self._log_files(env)
        return OK, {"keywords": [{"keyword": name, "path": "/var/log",
                                  "sourceNodeId": 0, "permission": "",
                                  "files": [f]} for name, f in
                                 ((f["name"], f) for f in files)]}

    def readlog(self, session, env_name, node_id=None, path=None, line=None,
                **_ignored):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        _, error = self._node_for_logs(env, node_id)
        if error:
            return error
        if not path:
            return INVALID_PARAMETER, {
                "error": "Parameter 'path' is required."}
        lines = self._log_lines(env, str(path))
        if lines is None:
            return INVALID_PARAMETER, {
                "error": f"invalid parameter [path] for method [ReadLog]"}
        try:
            take = int(line) if line not in (None, "") else len(lines)
        except (TypeError, ValueError):
            return INVALID_PARAMETER, {
                "error": "invalid parameter [line] for method [ReadLog]"}
        lines = lines[:max(0, take)]
        body = "".join(f"{l}\n" for l in lines)
        return OK, {"linescount": len(lines), "body": body}

    def getlogslist(self, session, env_name, node_id=None, **_ignored):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        _, error = self._node_for_logs(env, node_id)
        if error:
            return error
        # Live shape: directory entries and file groups with items.
        items = [{"path": f["path"], "size": f["size"], "name": f["name"],
                  "permission": f["permission"], "type": "FILE"}
                 for f in self._log_files(env)]
        return OK, {"array": [{"path": "/var/log", "name": "app",
                               "permission": "", "type": "GROUP",
                               "items": items}]}

    # -- container environment variables (live shapes) ----------------------

    def getcontainerenvvars(self, session, env_name, node_id=None, **_ignored):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        if node_id not in (None, ""):
            _, error = self._node_for_logs(env, node_id)
            if error:
                return error
        return OK, {"object": dict(env.env_vars)}

    def getcontainerenvvarsbygroup(self, session, env_name, node_group=None,
                                   **_ignored):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        return OK, {"object": dict(env.env_vars)}

    def setcontainerenvvarsbygroup(self, session, env_name, node_group=None,
                                   data=None, **_ignored):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        # Verified live shape: form data={"vars": {...}}.
        variables = data.get("vars") if isinstance(data, dict) else None
        if not isinstance(variables, dict):
            return INVALID_PARAMETER, {
                "error": "invalid parameter [data] for method "
                         "[SetContainerEnvVarsByGroup]"}
        env.env_vars = {str(k): str(v) for k, v in variables.items()}
        self._log(env, f"environment variables replaced "
                       f"({len(env.env_vars)} keys)")
        return OK, {"object": dict(env.env_vars)}

    def addcontainerenvvars(self, session, env_name, node_group=None,
                            vars=None, **_ignored):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        # Verified live shape: vars={"K":"V"}.
        if not isinstance(vars, dict) or not vars:
            return INVALID_PARAMETER, {
                "error": "invalid parameter [vars] for method "
                         "[AddContainerEnvVars]"}
        env.env_vars.update({str(k): "" if v is None else str(v)
                             for k, v in vars.items()})
        return OK, {"object": dict(env.env_vars)}

    def removecontainerenvvars(self, session, env_name, name=None,
                               node_group=None, **_ignored):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        if not name or name not in env.env_vars:
            return INVALID_PARAMETER, {
                "error": f"variable [{name}] not found"
            }
        del env.env_vars[str(name)]
        return OK, {"object": dict(env.env_vars)}

    # -- catalogs, quotas, SSL ----------------------------------------------

    def getcategories(self, session: Optional[str]):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        return OK, {"categories": [dict(c) for c in MARKETPLACE_CATEGORIES]}

    def getapplist(self, session: Optional[str], category: Optional[str] = None):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        apps = [dict(a) for a in MARKETPLACE_APPS]
        if category not in (None, ""):
            try:
                cat_id = int(category)
            except (TypeError, ValueError):
                return INVALID_PARAMETER, {
                    "error": "invalid parameter [category] for method [GetList]"
                }
            apps = [a for a in apps if a["category"] == cat_id]
        return OK, {"apps": apps}

    def gettemplates(self, session: Optional[str],
                     node_group: Optional[str] = None):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        templates = [dict(t) for t in TEMPLATES]
        if node_group:
            templates = [t for t in templates
                         if node_group in t.get("nodeGroups", [])]
        return OK, {"templates": templates}

    def getquotas(self, session: Optional[str]):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        self.advance_all()
        envs = [e for e in self.envs.values() if not e.removed]
        node_count = sum(len(e.nodes) for e in envs)
        cloudlets = sum(
            n.fixed_cloudlets + n.flexible_cloudlets
            for e in envs for n in e.nodes)
        return OK, {"quotas": {
            "environments": {"usage": len(envs), "limit": 10},
            "nodes": {"usage": node_count, "limit": 50},
            "cloudlets": {"usage": cloudlets, "limit": 400},
        }}

    def bindssl(self, session: Optional[str], env_name: Optional[str],
                ext_domain: Optional[str]):
        code = self._auth(session)
        if code != OK:
            return code, {"error": "session expired or invalid"}
        if not ext_domain or not DOMAIN_RE.match(str(ext_domain)):
            return INVALID_PARAMETER, {
                "error": "invalid parameter [extDomain] for method [BindSsl]"
            }
        code, env = self._get_env(env_name)
        if env is None:
            return code, {"error": f"environment [{env_name}] not found"}
        if env.status == "deleting" or env.removed:
            return DELETE_IN_PROGRESS, {
                "error": f"environment [{env_name}] is being deleted"
            }
        if env.status in TRANSIENT_STATES:
            return INVALID_PARAMETER, {
                "error": f"another operation is in progress for [{env_name}]"
            }
        if env.status != "running":
            return INVALID_PARAMETER, {
                "error": f"environment [{env_name}] is not running"
            }
        env.ssl = {
            "extDomain": str(ext_domain),
            "issuer": "Let's Encrypt",
            "issuedAt": self._now(),
        }
        self._log(env, f"Let's Encrypt SSL issued for {ext_domain}")
        return OK, {"envName": env.name, "extDomain": str(ext_domain),
                    "issuer": "Let's Encrypt", "status": "issued"}
