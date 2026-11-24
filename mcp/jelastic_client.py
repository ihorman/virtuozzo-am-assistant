"""Async Jelastic/VAP REST client (httpx) with session passthrough.

Design rules (from docs/PLAN.md):

- **Session passthrough**: every request carries the caller's ``session``
  token (query/form, exactly like the real API). The client never stores
  passwords; :meth:`JelasticClient.delete_env` forwards the typed
  confirmation ``passphrase=<envName>`` (required on VHC 8.15.1) and the
  account password once to ``deleteenv`` and keeps nothing.
- **Result-code mapping**: the platform answers HTTP 200 with
  ``{"result": <code>, ...}``; non-zero codes are raised as typed exceptions
  (:data:`RESULT_CODE_MAP`).
- **Fire-and-forget + observable-state polling**: mutations return as soon as
  the platform acknowledges them (create/start/stop/sleep/delete/deploy are
  async there). Completion is verified by polling ``getenvinfo``/``getenvs``
  (:meth:`wait_env_status`, :meth:`wait_env_gone`, :meth:`wait_nodes_ready`).
  A deadline hit raises :class:`OperationTimeout` carrying the last observed
  state — it never concludes the operation failed.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

# --- exceptions ------------------------------------------------------------


class JelasticError(Exception):
    """Base error: a non-zero platform result code (or transport failure)."""

    def __init__(self, code: Optional[int], message: str,
                 body: Optional[dict] = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.body = body or {}


class SessionExpiredError(JelasticError):
    """Session missing/unknown/expired (result 501 or 9) — re-authenticate."""


class AuthFailedError(JelasticError):
    """Authentication failed (result 701) — wrong login/password."""


class DeleteInProgressError(JelasticError):
    """Environment is already being deleted (result 2212)."""


class DomainUnavailableError(JelasticError):
    """Environment name / domain not available (result 2314)."""


class OperationFailedError(JelasticError):
    """Installation/operation failed on the platform (result 4109)."""


class JpsTimeoutError(OperationFailedError):
    """JPS cmd action exceeded the platform time limit (result 4132)."""


class ManifestError(JelasticError):
    """JPS manifest corrupted / not parsed (result 11001)."""


class EnvironmentNotFoundError(JelasticError):
    """Environment does not exist (live VHC result 11; legacy stands 2024)."""


class NodeNotFoundError(JelasticError):
    """Node does not exist (mock code 2025)."""


class InvalidParameterError(JelasticError):
    """Missing or invalid parameter (live result 3 / 'invalid parameter')."""


class MalformedResponseError(JelasticError):
    """Response was not the expected {result: ...} envelope (WAF page, proxy)."""


class RedirectBlockedError(JelasticError):
    """The platform answered with a redirect — refused.

    Redirects are never followed: the session token must not leave the
    pinned platform origin (docs/SECURITY.md T2)."""


class OperationTimeout(Exception):
    """A polling deadline or HTTP timeout elapsed.

    Deliberately NOT a :class:`JelasticError`: the operation may still be
    running on the platform. ``observed`` carries the last observed state so
    callers can report "still <status> after Ns" instead of concluding failure.
    """

    def __init__(self, message: str, *, env_name: Optional[str] = None,
                 target: Optional[list] = None, observed: Any = None):
        super().__init__(message)
        self.env_name = env_name
        self.target = target
        self.observed = observed


#: result code -> exception class (codes not listed raise :class:`JelasticError`)
#:
#: Codes verified live on VHC 8.15.1: 0 ok, 3 invalid parameter, 11 not found
#: (ghost env), 705 no rights, 724 "Confirmation failed" (deleteenv without
#: passphrase), 1102, 2002, 2314 domain taken, 2391, 4099 deploy failed.
RESULT_CODE_MAP: dict[int, type] = {
    3: InvalidParameterError,
    9: SessionExpiredError,
    11: EnvironmentNotFoundError,
    501: SessionExpiredError,
    701: AuthFailedError,
    724: InvalidParameterError,
    2212: DeleteInProgressError,
    2314: DomainUnavailableError,
    4099: OperationFailedError,
    4109: OperationFailedError,
    4132: JpsTimeoutError,
    11001: ManifestError,
    2024: EnvironmentNotFoundError,
    2025: NodeNotFoundError,
    2026: InvalidParameterError,
}

#: Numeric env ``status`` -> status name (the classic stable-state catalog).
#: Verified live on VHC 8.15.1: ``status: 1`` = running, and there is NO
#: ``statusName`` field at all - so the client synthesizes one. Any code
#: outside this map becomes ``status_<n>``: it can never match a wait target,
#: which is exactly "treat unknown values as not running".
STATUS_BY_CODE: dict[int, str] = {0: "stopped", 1: "running", 2: "sleep"}


def _normalize_env(env: dict) -> dict:
    """Return a copy of an env object with a usable ``statusName`` present."""
    env = dict(env)
    if not env.get("statusName"):
        status = env.get("status")
        if isinstance(status, bool) or not isinstance(status, int):
            env["statusName"] = None
        else:
            env["statusName"] = STATUS_BY_CODE.get(status, f"status_{status}")
    return env

# --- client ----------------------------------------------------------------

_ARCHIVE_SUFFIXES = (".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z",
                     ".war", ".jar", ".ear")


def _archive_file_name(url: str) -> str:
    """Derive a safe ``fileName`` for a deploy URL (the live API wants one)."""
    name = urlparse(url).path.rsplit("/", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9._-]", "", name)
    if not name:
        name = "archive.zip"
    if not name.lower().endswith(_ARCHIVE_SUFFIXES):
        name += ".zip"
    return name


class JelasticClient:
    """Async client for the Jelastic REST subset used by Application Management Assistant.

    ``host`` is the platform base URL (e.g. ``https://app.example.com``);
    requests go to ``<host>/1.0/<service>/rest/<method>``. Pass ``transport``
    to run against an ASGI app in tests (no real network).
    """

    def __init__(self, host: str, session: Optional[str] = None, *,
                 timeout: float = 30.0, transport: Optional[httpx.AsyncBaseTransport] = None,
                 poll_interval: float = 0.25):
        host = host.rstrip("/")
        if not host.startswith(("http://", "https://")):
            host = "https://" + host
        self.host = host
        self.session = session
        self.poll_interval = poll_interval
        self._http = httpx.AsyncClient(
            base_url=f"{host}/1.0/",
            timeout=timeout,
            transport=transport,
            follow_redirects=False,  # token pinning: never leave self.host
        )

    # -- lifecycle ----------------------------------------------------------

    @classmethod
    def from_headers(cls, headers: dict, **kwargs) -> "JelasticClient":
        """Build from the extension contract headers (X-Jelastic-*)."""
        host = headers.get("X-Jelastic-Host") or headers.get("x-jelastic-host")
        session = headers.get("X-Jelastic-Session") or headers.get("x-jelastic-session")
        if not host or not session:
            raise InvalidParameterError(
                None, "X-Jelastic-Host and X-Jelastic-Session headers are required")
        return cls(host, session, **kwargs)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "JelasticClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # -- transport ----------------------------------------------------------

    def _raise_for_result(self, body: dict) -> dict:
        code = body.get("result")
        if code is None:
            raise MalformedResponseError(
                None, "response has no 'result' field", body)
        if code != 0:
            message = body.get("error") or body.get("errorText") or "platform error"
            exc_cls = RESULT_CODE_MAP.get(code, JelasticError)
            raise exc_cls(code, message, body)
        # VHC/hx-core nests operation errors under 'response' chains even when
        # the outer result is 0 (verified live: {result:0, response:{result:3,
        # error:...}}). Walk the chain and fail on the first nested error.
        node: Any = body
        for _ in range(5):
            if isinstance(node, dict):
                node = node.get("response")
            elif isinstance(node, list):
                nested = next((n for n in node if isinstance(n, dict)
                               and n.get("result") not in (None, 0)), None)
                node = nested if nested is not None else None
            else:
                node = None
            if isinstance(node, dict) and node.get("result") not in (None, 0):
                ncode = node.get("result")
                message = (node.get("error") or node.get("errorText")
                           or "platform error")
                exc_cls = RESULT_CODE_MAP.get(ncode, JelasticError)
                if "invalid parameter" in str(message).lower():
                    exc_cls = InvalidParameterError
                raise exc_cls(ncode, message, node)
            if node is None:
                break
        return body

    async def _request(self, path: str, params: Optional[dict] = None, *,
                       json_body: Optional[dict] = None) -> dict:
        """One platform call. GET with query params, or POST with JSON body.

        ``session`` is injected into whichever channel carries the params.
        """
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            if json_body is not None:
                # VHC/hx-core reads POST parameters from the form channel only
                # (verified live: a JSON body yields "invalid parameter
                # [session]"). Nested values (env, nodes, vars) travel as JSON
                # strings inside form fields - the classic Jelastic shape.
                form = {k: v for k, v in dict(json_body).items()
                        if v is not None}
                if self.session is not None:
                    form.setdefault("session", self.session)
                data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
                        for k, v in form.items()}
                response = await self._http.post(path, data=data)
            else:
                if self.session is not None:
                    params.setdefault("session", self.session)
                response = await self._http.get(path, params=params)
        except httpx.TimeoutException as exc:
            # Never conclude failure from a client-side timeout.
            raise OperationTimeout(
                f"HTTP request to {path} timed out; the operation may still "
                f"be running on the platform") from exc
        if 300 <= response.status_code < 400:
            # Token pinning: a redirect would move the session token to
            # another origin. Refuse and surface it plainly.
            raise RedirectBlockedError(
                None,
                f"the platform answered HTTP {response.status_code} with a "
                f"redirect; redirects are refused so the session token "
                f"cannot leave the platform origin")
        try:
            body = response.json()
        except ValueError as exc:
            raise MalformedResponseError(
                None,
                f"non-JSON response (HTTP {response.status_code}) from {path}",
                {"text": response.text[:500]}) from exc
        if not isinstance(body, dict):
            raise MalformedResponseError(None, "response is not a JSON object",
                                         {"text": str(body)[:500]})
        return self._raise_for_result(body)

    @staticmethod
    def _bool(value: Optional[bool]) -> Optional[str]:
        if value is None:
            return None
        return "true" if value else "false"

    # -- auth ---------------------------------------------------------------

    async def signin(self, login: str, password: str) -> dict:
        """Authenticate and adopt the returned session token (stateless use
        stays possible: pass ``session=`` to the constructor instead)."""
        body = await self._request(
            "users/authentication/rest/signin",
            params={"login": login, "password": password})
        self.session = body["session"]
        return body

    async def signout(self) -> dict:
        body = await self._request("users/authentication/rest/signout")
        self.session = None
        return body

    # -- read tools ---------------------------------------------------------

    async def list_envs(self) -> list:
        """All environments as plain env dicts (status normalized).

        Classic Jelastic nests each env under ``infos[].env``; VHC 8.15.1
        returns flat summaries - both are unwrapped to the same shape.
        """
        body = await self._request("environment/control/rest/getenvs")
        envs = []
        for entry in body.get("infos", []) or []:
            if not isinstance(entry, dict):
                continue
            nested = entry.get("env")
            envs.append(_normalize_env(nested if isinstance(nested, dict)
                                       else entry))
        return envs

    async def env_info(self, env_name: str) -> dict:
        """Full environment info as one dict: the env object with ``nodes``
        and ``nodeGroups`` attached (``statusName`` normalized).

        Accepts every shape in the family: VHC 8.15.1 returns the objects
        FLAT at the top level, classic Jelastic nests them under ``info``,
        and some stands embed ``nodes`` inside the env object itself.
        """
        body = await self._request("environment/control/rest/getenvinfo",
                                   params={"envName": env_name})
        nested = body.get("info")
        info: dict = nested if isinstance(nested, dict) else body
        env_obj = info.get("env")
        if isinstance(env_obj, dict):
            env: dict = env_obj
        else:
            env = {k: v for k, v in info.items()
                   if k not in ("result", "nodes", "nodeGroups")}
        nodes = info.get("nodes") or env.get("nodes") or []
        node_groups = info.get("nodeGroups") or env.get("nodeGroups") or []
        merged = {k: v for k, v in env.items()
                  if k not in ("nodes", "nodeGroups")}
        merged["nodes"] = list(nodes)
        merged["nodeGroups"] = list(node_groups)
        return _normalize_env(merged)

    async def env_stats(self, env_name: str) -> list:
        """Per-node resource stats (``getsumstat`` - ``getenvstat`` 404s live).

        The live envelope carries one entry per node plus an aggregate row
        with ``nodeid == "sum"``; the aggregate is not a node and is dropped.
        """
        body = await self._request("environment/control/rest/getsumstat",
                                   params={"envName": env_name})
        stats = body.get("stats") or body.get("nodes") or []
        return [s for s in stats
                if str(s.get("nodeid", s.get("nodeId", ""))) != "sum"]

    async def _resolve_node_id(self, env_name: str,
                               node_id: Optional[int]) -> int:
        """Log APIs require a ``nodeId`` live; default to the master node."""
        if node_id is not None:
            return node_id
        info = await self.env_info(env_name)
        nodes = info.get("nodes") or []
        node = next((n for n in nodes if n.get("ismaster")), None) \
            or (nodes[0] if nodes else None)
        if node is None or node.get("id") is None:
            raise NodeNotFoundError(
                None, f"environment [{env_name}] has no nodes to read logs "
                      f"from", {"envName": env_name})
        return node["id"]

    async def get_logs(self, env_name: str, *, node_id: Optional[int] = None,
                       limit: Optional[int] = None) -> list:
        """Log-file listing for a node (``getlogs``; requires ``nodeId`` live).

        Returns the keyword/file groups (``keywords`` on VHC 8.15.1). The
        content of one file comes from :meth:`read_log`.
        """
        body = await self._request(
            "environment/control/rest/getlogs",
            params={"envName": env_name,
                    "nodeId": await self._resolve_node_id(env_name, node_id)})
        groups = body.get("keywords") or body.get("logs") or []
        if limit is not None:
            groups = groups[:max(0, int(limit))]
        return groups

    async def read_log(self, env_name: str, path: str, *,
                       node_id: Optional[int] = None,
                       line: Optional[int] = None) -> dict:
        """Read the head of one log file (``readlog``).

        Live shape: ``path=/var/log/...&line=N`` -> ``{linescount, body}``.
        """
        body = await self._request(
            "environment/control/rest/readlog",
            params={"envName": env_name,
                    "nodeId": await self._resolve_node_id(env_name, node_id),
                    "path": path, "line": line})
        return {"path": path, "linescount": body.get("linescount"),
                "body": body.get("body", "")}

    async def get_logs_list(self, env_name: str, *,
                            node_id: Optional[int] = None) -> list:
        """Log directories and file groups for a node (``getlogslist``)."""
        body = await self._request(
            "environment/control/rest/getlogslist",
            params={"envName": env_name,
                    "nodeId": await self._resolve_node_id(env_name, node_id)})
        return body.get("array") or []

    async def get_env_variables(self, env_name: str, *,
                                node_group: str = "cp",
                                node_id: Optional[int] = None) -> dict:
        """Container env vars: ``getcontainerenvvars`` by node id, or
        ``getcontainerenvvarsbygroup`` by node group (live: ``{"object":
        {...}}``). ``getenvvariables`` does not exist on VHC 8.15.1."""
        if node_id is not None:
            body = await self._request(
                "environment/control/rest/getcontainerenvvars",
                params={"envName": env_name, "nodeId": node_id})
        else:
            body = await self._request(
                "environment/control/rest/getcontainerenvvarsbygroup",
                params={"envName": env_name, "nodeGroup": node_group})
        return body.get("object") or body.get("vars") or {}

    # -- catalogs / quotas / SSL (deploy wizard awareness) -------------------

    async def marketplace_categories(self) -> list:
        body = await self._request("marketplace/app/rest/getcategories")
        return body.get("categories", [])

    async def marketplace_apps(self, category: Optional[int] = None) -> list:
        body = await self._request("marketplace/app/rest/getlist",
                                   params={"category": category})
        return body.get("apps", [])

    async def list_templates(self, node_group: Optional[str] = None) -> list:
        body = await self._request("environment/control/rest/gettemplates",
                                   params={"nodeGroup": node_group})
        return body.get("templates", [])

    async def get_quotas(self) -> dict:
        body = await self._request("billing/account/rest/getquotas")
        return body.get("quotas", {})

    async def bind_ssl(self, env_name: str, ext_domain: str) -> dict:
        """Let's Encrypt flow (environment/binder/rest/bindssl)."""
        return await self._request("environment/binder/rest/bindssl",
                                   params={"envName": env_name,
                                           "extDomain": ext_domain})

    # -- mutating tools (fire-and-forget: return on acknowledgement) --------

    async def create_env(self, env_name: str, *, nodes: Optional[list] = None,
                         engine: Optional[str] = None,
                         region: Optional[str] = None,
                         display_name: Optional[str] = None) -> dict:
        env = {"shortdomain": env_name}
        # Region names are platform-specific (verified live: VHC rejects
        # "default"); only send one when the caller explicitly has a region.
        if region is not None:
            env["region"] = region
        if engine is not None:
            env["engine"] = engine
        if display_name is not None:
            env["displayName"] = display_name
        return await self._request(
            "environment/control/rest/createenvironment",
            json_body={"env": env, "nodes": nodes or [
                {"nodeType": "cp", "nodeGroup": "cp",
                 "fixedCloudlets": 1, "flexibleCloudlets": 7}]})

    async def start_env(self, env_name: str) -> dict:
        return await self._request("environment/control/rest/startenv",
                                   params={"envName": env_name})

    async def stop_env(self, env_name: str) -> dict:
        return await self._request("environment/control/rest/stopenv",
                                   params={"envName": env_name})

    async def sleep_env(self, env_name: str) -> dict:
        return await self._request("environment/control/rest/sleepenv",
                                   params={"envName": env_name})

    async def delete_env(self, env_name: str, password: Optional[str] = None,
                         *, wait: bool = True, timeout: float = 120.0) -> dict:
        """Delete an environment (POST ``deleteenv``).

        VHC 8.15.1 requires the typed confirmation ``passphrase=<envName>``
        (result 724 "Confirmation failed" without it); the account password is
        sent along when the caller has one. Neither is stored. A ``2212``
        (deletion already in progress) is tolerated: it means the environment
        is on its way out, which is the goal. With ``wait=True`` the call
        returns only once the environment is gone."""
        form: dict = {"envName": env_name, "passphrase": env_name}
        if password:
            form["password"] = password
        try:
            body = await self._request(
                "environment/control/rest/deleteenv", json_body=form)
        except DeleteInProgressError:
            body = {"result": 0, "envName": env_name, "statusName": "deleting",
                    "alreadyInProgress": True}
        if wait:
            await self.wait_env_gone(env_name, timeout=timeout)
        return body

    async def scale_node(self, env_name: str, node_id: int,
                         fixed_cloudlets: int, flexible_cloudlets: int) -> dict:
        return await self._request(
            "environment/control/rest/setcloudletscountbyid",
            json_body={"envName": env_name, "nodeId": node_id,
                       "fixedCloudlets": fixed_cloudlets,
                       "flexibleCloudlets": flexible_cloudlets})

    async def change_topology(self, env_name: str, nodes: list, *,
                              region: str = "", sslstate: bool = False) -> dict:
        """Change the topology (add/remove node groups, resize).

        Verified live shape: form-encoded POST with the ``env`` descriptor
        ``{"region": "", "sslstate": false}`` + the ``nodes`` specs as a JSON
        string; a bare ``envName`` yields "invalid parameter [env]".
        """
        return await self._request(
            "environment/control/rest/changetopology",
            json_body={"envName": env_name,
                       "env": {"region": region, "sslstate": sslstate},
                       "nodes": nodes})

    async def apply_node_group_data(self, env_name: str, node_group: str,
                                    *, count: Optional[int] = None,
                                    fixed_cloudlets: Optional[int] = None,
                                    flexible_cloudlets: Optional[int] = None
                                    ) -> dict:
        """Scale an EXISTING node group (verified live shape: ``nodeGroup`` +
        form ``data={"count","fixedCloudlets","flexibleCloudlets"}``).

        Adding nodes is not this method's job (``editnodegroup`` answers 1102
        for adds) - use :meth:`change_topology` to add nodes.
        """
        data: dict = {}
        if count is not None:
            data["count"] = count
        if fixed_cloudlets is not None:
            data["fixedCloudlets"] = fixed_cloudlets
        if flexible_cloudlets is not None:
            data["flexibleCloudlets"] = flexible_cloudlets
        return await self._request(
            "environment/control/rest/applynodegroupdata",
            json_body={"envName": env_name, "nodeGroup": node_group,
                       "data": data})

    async def deploy_archive(self, env_name: str, *, context: str = "ROOT",
                             url: Optional[str] = None,
                             archive: Optional[str] = None,
                             node_group: Optional[str] = None) -> dict:
        """Deploy an archive into an environment context.

        The live wire shape is ``fileUrl`` + ``fileName`` (VHC 8.15.1 rejects
        ``url``/``archive``): a ``url`` maps to ``fileUrl`` with a derived
        ``fileName``, and a pre-uploaded ``archive`` name maps to ``fileName``.
        """
        form: dict = {"envName": env_name, "context": context,
                      "nodeGroup": node_group}
        if url:
            form["fileUrl"] = url
            form["fileName"] = _archive_file_name(url)
        elif archive:
            form["fileName"] = archive
        return await self._request(
            "environment/deployment/rest/deployarchive", json_body=form)

    async def install_jps(self, env_name: str, jps: str, *,
                          settings: Optional[Any] = None,
                          skip_node_emails: bool = True) -> dict:
        """Install a JPS manifest (URL or inline YAML) into an environment."""
        if settings is not None and not isinstance(settings, str):
            settings = json.dumps(settings)
        return await self._request(
            "marketplace/jps/rest/install",
            params={"envName": env_name, "jps": jps, "settings": settings,
                    "skipNodeEmails": self._bool(skip_node_emails)})

    async def set_env_variables(self, env_name: str, variables: dict, *,
                                node_group: str = "cp") -> dict:
        """Replace a node group's container env vars (verified live shape:
        ``setcontainerenvvarsbygroup`` with form ``data={"vars": {...}}``;
        ``setenvvariables`` does not exist on VHC 8.15.1)."""
        body = await self._request(
            "environment/control/rest/setcontainerenvvarsbygroup",
            json_body={"envName": env_name, "nodeGroup": node_group,
                       "data": {"vars": dict(variables)}})
        return body.get("object") or body.get("vars") or {}

    async def add_env_variable(self, env_name: str, name: str,
                               value: str, *, node_group: str = "cp") -> dict:
        """Add one container env var (verified live shape:
        ``addcontainerenvvars`` with form ``vars={"K":"V"}``)."""
        body = await self._request(
            "environment/control/rest/addcontainerenvvars",
            json_body={"envName": env_name, "nodeGroup": node_group,
                       "vars": {name: value}})
        return body.get("object") or body.get("vars") or {}

    async def remove_env_variable(self, env_name: str, name: str, *,
                                  node_group: str = "cp") -> dict:
        """Remove one container env var (``removecontainerenvvars``)."""
        body = await self._request(
            "environment/control/rest/removecontainerenvvars",
            params={"envName": env_name, "nodeGroup": node_group,
                    "name": name})
        return body.get("object") or body.get("vars") or {}

    # -- observable-state polling -------------------------------------------

    async def wait_env_status(self, env_name: str, *statuses: str,
                              timeout: float = 120.0,
                              poll_interval: Optional[float] = None) -> dict:
        """Poll ``getenvinfo`` until ``statusName`` is one of ``statuses``.

        On deadline raises :class:`OperationTimeout` with the last observed
        environment info — the operation is NOT declared failed.
        """
        interval = poll_interval if poll_interval is not None else self.poll_interval
        deadline = time.monotonic() + timeout
        observed: Any = None
        while True:
            observed = await self.env_info(env_name)  # EnvironmentNotFound re-raises
            if observed.get("statusName") in statuses:
                return observed
            if time.monotonic() >= deadline:
                raise OperationTimeout(
                    f"environment [{env_name}] still "
                    f"[{observed.get('statusName')}] after {timeout}s "
                    f"(wanted {sorted(statuses)}); the operation may still "
                    f"be running on the platform",
                    env_name=env_name, target=list(statuses), observed=observed)
            await asyncio.sleep(interval)

    async def wait_env_gone(self, env_name: str, *, timeout: float = 120.0,
                            poll_interval: Optional[float] = None) -> None:
        """Poll until the environment no longer exists (delete verification)."""
        interval = poll_interval if poll_interval is not None else self.poll_interval
        deadline = time.monotonic() + timeout
        observed: Any = None
        while True:
            try:
                observed = await self.env_info(env_name)
            except EnvironmentNotFoundError:
                return
            if time.monotonic() >= deadline:
                raise OperationTimeout(
                    f"environment [{env_name}] still exists after {timeout}s "
                    f"(status [{observed.get('statusName')}]); deletion may "
                    f"still be running on the platform",
                    env_name=env_name, target=["gone"], observed=observed)
            await asyncio.sleep(interval)

    async def wait_nodes_ready(self, env_name: str, *, timeout: float = 120.0,
                               poll_interval: Optional[float] = None) -> dict:
        """Poll until every node reports ready (topology/scaling done).

        Node status is string ``"ok"`` on classic stands and numeric ``1`` on
        VHC 8.15.1 - both count as ready.
        """
        interval = poll_interval if poll_interval is not None else self.poll_interval
        deadline = time.monotonic() + timeout
        observed: Any = None
        while True:
            observed = await self.env_info(env_name)
            nodes = observed.get("nodes", [])
            if nodes and all(n.get("status") in ("ok", "running", 1)
                             for n in nodes):
                return observed
            if time.monotonic() >= deadline:
                raise OperationTimeout(
                    f"nodes of [{env_name}] not ready after {timeout}s; "
                    f"topology work may still be running on the platform",
                    env_name=env_name, target=["nodes_ok"], observed=observed)
            await asyncio.sleep(interval)
