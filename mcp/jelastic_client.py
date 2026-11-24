"""Async Jelastic/VAP REST client (httpx) with session passthrough.

Design rules (from docs/PLAN.md):

- **Session passthrough**: every request carries the caller's ``session``
  token (query/form/JSON, exactly like the real API). The client never stores
  passwords; :meth:`JelasticClient.delete_env` forwards the account password
  once to ``deleteenv`` and keeps nothing.
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
import time
from typing import Any, Optional

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
    """Environment does not exist (mock code 2024 / text-mapped on real stands)."""


class NodeNotFoundError(JelasticError):
    """Node does not exist (mock code 2025)."""


class InvalidParameterError(JelasticError):
    """Missing or invalid parameter (mock code 2026 / real 'invalid parameter')."""


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
RESULT_CODE_MAP: dict[int, type] = {
    9: SessionExpiredError,
    501: SessionExpiredError,
    701: AuthFailedError,
    2212: DeleteInProgressError,
    2314: DomainUnavailableError,
    4109: OperationFailedError,
    4132: JpsTimeoutError,
    11001: ManifestError,
    2024: EnvironmentNotFoundError,
    2025: NodeNotFoundError,
    2026: InvalidParameterError,
}

# --- client ----------------------------------------------------------------


class JelasticClient:
    """Async client for the Jelastic REST subset used by VAP Copilot.

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
        if code == 0:
            return body
        message = body.get("error") or body.get("errorText") or "platform error"
        exc_cls = RESULT_CODE_MAP.get(code, JelasticError)
        raise exc_cls(code, message, body)

    async def _request(self, path: str, params: Optional[dict] = None, *,
                       json_body: Optional[dict] = None) -> dict:
        """One platform call. GET with query params, or POST with JSON body.

        ``session`` is injected into whichever channel carries the params.
        """
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            if json_body is not None:
                body = dict(json_body)
                if self.session is not None:
                    body.setdefault("session", self.session)
                response = await self._http.post(path, json=body)
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
        body = await self._request("environment/control/rest/getenvs")
        return body.get("infos", [])

    async def env_info(self, env_name: str) -> dict:
        body = await self._request("environment/control/rest/getenvinfo",
                                   params={"envName": env_name})
        return body["env"]

    async def env_stats(self, env_name: str) -> list:
        body = await self._request("environment/control/rest/getenvstat",
                                   params={"envName": env_name})
        return body.get("nodes", [])

    async def get_logs(self, env_name: str, *, limit: Optional[int] = None) -> list:
        body = await self._request("environment/control/rest/getlogs",
                                   params={"envName": env_name, "limit": limit})
        return body.get("logs", [])

    async def get_env_variables(self, env_name: str) -> dict:
        body = await self._request("environment/control/rest/getenvvariables",
                                   params={"envName": env_name})
        return body.get("vars", {})

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
                         engine: Optional[str] = None, region: str = "default",
                         display_name: Optional[str] = None) -> dict:
        env = {"shortdomain": env_name, "region": region}
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

    async def delete_env(self, env_name: str, password: str, *,
                         wait: bool = True, timeout: float = 120.0) -> dict:
        """Delete an environment. The account password is forwarded once and
        never stored. A ``2212`` (deletion already in progress) is tolerated:
        it means the environment is on its way out, which is the goal. With
        ``wait=True`` the call returns only once the environment is gone."""
        try:
            body = await self._request(
                "environment/control/rest/deleteenv",
                params={"envName": env_name, "password": password})
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
            params={"envName": env_name, "nodeId": node_id,
                    "fixedCloudlets": fixed_cloudlets,
                    "flexibleCloudlets": flexible_cloudlets})

    async def change_topology(self, env_name: str, nodes: list) -> dict:
        return await self._request(
            "environment/control/rest/changetopology",
            json_body={"envName": env_name, "nodes": nodes})

    async def deploy_archive(self, env_name: str, *, context: str = "ROOT",
                             url: Optional[str] = None,
                             archive: Optional[str] = None,
                             node_group: Optional[str] = None) -> dict:
        return await self._request(
            "environment/deployment/rest/deployarchive",
            params={"envName": env_name, "context": context, "url": url,
                    "archive": archive, "nodeGroup": node_group})

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

    async def set_env_variables(self, env_name: str, variables: dict) -> dict:
        body = await self._request(
            "environment/control/rest/setenvvariables",
            json_body={"envName": env_name, "vars": dict(variables)})
        return body.get("vars", {})

    async def add_env_variable(self, env_name: str, name: str,
                               value: str) -> dict:
        body = await self._request(
            "environment/control/rest/addcontainerenvvars",
            params={"envName": env_name, "name": name, "value": value})
        return body.get("vars", {})

    async def remove_env_variable(self, env_name: str, name: str) -> dict:
        body = await self._request(
            "environment/control/rest/removecontainerenvvars",
            params={"envName": env_name, "name": name})
        return body.get("vars", {})

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
        """Poll until every node reports status ``ok`` (topology/scaling done)."""
        interval = poll_interval if poll_interval is not None else self.poll_interval
        deadline = time.monotonic() + timeout
        observed: Any = None
        while True:
            observed = await self.env_info(env_name)
            nodes = observed.get("nodes", [])
            if nodes and all(n.get("status") == "ok" for n in nodes):
                return observed
            if time.monotonic() >= deadline:
                raise OperationTimeout(
                    f"nodes of [{env_name}] not ready after {timeout}s; "
                    f"topology work may still be running on the platform",
                    env_name=env_name, target=["nodes_ok"], observed=observed)
            await asyncio.sleep(interval)
