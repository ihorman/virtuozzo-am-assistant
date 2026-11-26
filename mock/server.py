"""HTTP layer of the mock Jelastic REST API (FastAPI).

Implements the request conventions of the real platform:

- Base path ``/1.0/<service>/rest/<method>``.
- Every call carries ``session=<token>`` (query, form or JSON body).
- Some methods take a JSON body (``createenvironment``, ``changetopology``);
  everything else accepts GET query params or POST form params.
- Response envelope: ``{"result": 0, ...}`` on success,
  ``{"result": <code>, "error": "..."}`` on failure. Errors are HTTP 200 with
  a non-zero ``result`` — exactly like the real API.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .codes import INVALID_PARAMETER, OK
from .state import MockJelastic

API_PREFIX = "/1.0/"


async def _collect_params(request: Request) -> dict:
    """Merge JSON body, form fields and query params (query wins)."""
    params: dict[str, Any] = {}
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        if isinstance(body, dict):
            params.update(body)
    elif "multipart/form-data" in content_type or \
            "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        for key, value in form.multi_items():
            # File uploads (deployarchive): keep the filename only.
            filename = getattr(value, "filename", None)
            value = filename if filename is not None else value
            # Classic Jelastic carries structured params (env, nodes, vars) as
            # JSON strings inside form fields; decode objects/arrays.
            if isinstance(value, str) and value[:1] in "[{":
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            params[key] = value
    for key, value in request.query_params.multi_items():
        params[key] = value
    return params


def _envelope(code: int, payload: dict) -> JSONResponse:
    if code == OK:
        return JSONResponse({"result": OK, **payload})
    return JSONResponse({"result": code, "error": payload.get("error", "unknown error")})


def create_app(state: Optional[MockJelastic] = None) -> FastAPI:
    """Build the mock API around a :class:`MockJelastic` state machine."""
    state = state or MockJelastic()
    app = FastAPI(title="Mock Jelastic API", docs_url=None, redoc_url=None)
    app.state.jelastic = state

    def dispatch(method: str, p: dict) -> JSONResponse:
        st: MockJelastic = app.state.jelastic
        st.calls.append(method)  # recorded for routing assertions in tests
        if method == "users/authentication/rest/signin":
            return _envelope(*st.signin(p.get("login"), p.get("password")))
        if method == "users/authentication/rest/signout":
            return _envelope(*st.signout(p.get("session")))
        if method == "authentication/signin":  # legacy alias
            return _envelope(*st.signin(p.get("login"), p.get("password")))

        if method == "environment/control/rest/getenvs":
            return _envelope(*st.getenvs(p.get("session")))
        if method == "environment/control/rest/getenvinfo":
            return _envelope(*st.getenvinfo(p.get("session"), p.get("envName")))
        # Live method name is getsumstat (getenvstat 404s on VHC 8.15.1).
        if method == "environment/control/rest/getsumstat":
            return _envelope(*st.getsumstat(p.get("session"), p.get("envName")))
        if method in ("environment/control/rest/createenvironment",
                      "environment/control/rest/createenv"):
            return _envelope(*st.createenvironment(
                p.get("session"), p.get("env"), p.get("nodes")))
        if method == "environment/control/rest/startenv":
            return _envelope(*st.startenv(p.get("session"), p.get("envName")))
        if method == "environment/control/rest/stopenv":
            return _envelope(*st.stopenv(p.get("session"), p.get("envName")))
        if method == "environment/control/rest/sleepenv":
            return _envelope(*st.sleepenv(p.get("session"), p.get("envName")))
        if method == "environment/control/rest/deleteenv":
            return _envelope(*st.deleteenv(
                p.get("session"), p.get("envName"), p.get("password"),
                p.get("passphrase")))
        if method == "environment/control/rest/changetopology":
            return _envelope(*st.changetopology(
                p.get("session"), p.get("envName"), p.get("nodes"),
                p.get("env")))
        if method == "environment/control/rest/applynodegroupdata":
            return _envelope(*st.applynodegroupdata(
                p.get("session"), p.get("envName"), p.get("nodeGroup"),
                p.get("data")))
        if method == "environment/control/rest/setcloudletscountbyid":
            return _envelope(*st.setcloudletscountbyid(
                p.get("session"), p.get("envName"), p.get("nodeId"),
                p.get("fixedCloudlets"), p.get("flexibleCloudlets")))

        if method in ("environment/deployment/rest/deployarchive",
                      "environment/control/rest/deployapp"):
            return _envelope(*st.deployarchive(
                p.get("session"), p.get("envName"), p.get("context"),
                p.get("fileUrl"), p.get("fileName"), p.get("nodeGroup")))

        # VCS deployment (deploy-anything wizard, git path): register the
        # repo, deploy it, then fetch recent changes (update).
        if method == "environment/deployment/rest/addrepo":
            return _envelope(*st.addrepo(
                p.get("session"), p.get("envName"), p.get("context"),
                p.get("url"), p.get("branch")))
        if method == "environment/deployment/rest/deploy":
            return _envelope(*st.vcsdeploy(
                p.get("session"), p.get("envName"), p.get("context")))
        if method == "environment/deployment/rest/update":
            return _envelope(*st.vcsupdate(
                p.get("session"), p.get("envName"), p.get("context")))

        if method == "marketplace/jps/rest/install":
            return _envelope(*st.install_jps(
                p.get("session"), p.get("jps"), p.get("envName"),
                p.get("settings"), p.get("skipNodeEmails")))

        if method == "environment/control/rest/getlogs":
            return _envelope(*st.getlogs(
                p.get("session"), p.get("envName"), p.get("nodeId")))
        if method == "environment/control/rest/readlog":
            return _envelope(*st.readlog(
                p.get("session"), p.get("envName"), p.get("nodeId"),
                p.get("path"), p.get("line")))
        if method == "environment/control/rest/getlogslist":
            return _envelope(*st.getlogslist(
                p.get("session"), p.get("envName"), p.get("nodeId")))
        if method == "environment/control/rest/getcontainerenvvars":
            return _envelope(*st.getcontainerenvvars(
                p.get("session"), p.get("envName"), p.get("nodeId")))
        if method == "environment/control/rest/getcontainerenvvarsbygroup":
            return _envelope(*st.getcontainerenvvarsbygroup(
                p.get("session"), p.get("envName"), p.get("nodeGroup")))
        if method == "environment/control/rest/setcontainerenvvarsbygroup":
            return _envelope(*st.setcontainerenvvarsbygroup(
                p.get("session"), p.get("envName"), p.get("nodeGroup"),
                p.get("data")))
        if method == "environment/control/rest/addcontainerenvvars":
            return _envelope(*st.addcontainerenvvars(
                p.get("session"), p.get("envName"), p.get("nodeGroup"),
                p.get("vars")))
        if method == "environment/control/rest/removecontainerenvvars":
            return _envelope(*st.removecontainerenvvars(
                p.get("session"), p.get("envName"), p.get("name"),
                p.get("nodeGroup")))

        if method == "marketplace/app/rest/getcategories":
            return _envelope(*st.getcategories(p.get("session")))
        if method == "marketplace/app/rest/getlist":
            return _envelope(*st.getapplist(p.get("session"), p.get("category")))
        if method == "environment/control/rest/gettemplates":
            return _envelope(*st.gettemplates(
                p.get("session"), p.get("nodeGroup")))
        if method == "billing/account/rest/getquotas":
            return _envelope(*st.getquotas(p.get("session")))
        if method == "environment/binder/rest/bindssl":
            return _envelope(*st.bindssl(
                p.get("session"), p.get("envName"), p.get("extDomain")))

        # T13 coverage endpoints (sections 4.6-4.15 of the method catalog).
        # Domains: bindextdomain / removeextdomain (the catalog has no
        # "unbindextdomain" - removeextdomain is the exact name).
        if method == "environment/binder/rest/bindextdomain":
            return _envelope(*st.bindextdomain(
                p.get("session"), p.get("envName"), p.get("extDomain")))
        if method == "environment/binder/rest/removeextdomain":
            return _envelope(*st.removeextdomain(
                p.get("session"), p.get("envName"), p.get("extDomain")))

        # Clone / restart.
        if method == "environment/control/rest/cloneenv":
            return _envelope(*st.cloneenv(
                p.get("session"), p.get("srcEnvName"), p.get("dstEnvName")))
        if method == "environment/control/rest/restartnodes":
            return _envelope(*st.restartnodes(
                p.get("session"), p.get("envName"), p.get("nodeGroup")))
        if method == "environment/control/rest/restartnodebyid":
            return _envelope(*st.restartnodebyid(
                p.get("session"), p.get("envName"), p.get("nodeId")))

        # Ports & endpoints.
        if method == "environment/control/rest/getendpoints":
            return _envelope(*st.getendpoints(
                p.get("session"), p.get("envName")))
        if method == "environment/control/rest/addendpoint":
            return _envelope(*st.addendpoint(
                p.get("session"), p.get("envName"), p.get("nodeId"),
                p.get("name"), p.get("privatePort"), p.get("protocol")))
        if method == "environment/control/rest/removeendpoint":
            return _envelope(*st.removeendpoint(
                p.get("session"), p.get("envName"), p.get("id")))

        # Container firewall (environment/security/rest/*).
        if method == "environment/security/rest/getrules":
            return _envelope(*st.getrules(
                p.get("session"), p.get("envName"), p.get("nodeId")))
        if method == "environment/security/rest/addrule":
            return _envelope(*st.addrule(
                p.get("session"), p.get("envName"), p.get("nodeId"),
                p.get("protocol"), p.get("ports"), p.get("src"),
                p.get("action"), p.get("priority")))
        if method == "environment/security/rest/removerule":
            return _envelope(*st.removerule(
                p.get("session"), p.get("envName"), p.get("id")))

        # File manager (environment/file/rest/*).
        if method == "environment/file/rest/getlist":
            return _envelope(*st.getlist(
                p.get("session"), p.get("envName"), p.get("nodeId"),
                p.get("path")))
        if method == "environment/file/rest/read":
            return _envelope(*st.read(
                p.get("session"), p.get("envName"), p.get("nodeId"),
                p.get("path")))
        if method == "environment/file/rest/upload":
            return _envelope(*st.upload(
                p.get("session"), p.get("envName"), p.get("nodeId"),
                p.get("path"), p.get("body")))
        if method == "environment/file/rest/write":
            return _envelope(*st.write(
                p.get("session"), p.get("envName"), p.get("nodeId"),
                p.get("path"), p.get("body")))
        if method == "environment/file/rest/replaceinbody":
            return _envelope(*st.replaceinbody(
                p.get("session"), p.get("envName"), p.get("nodeId"),
                p.get("path"), p.get("search"), p.get("replace")))

        # Autoscaling triggers (environment/trigger/rest/*).
        if method == "environment/trigger/rest/gettriggers":
            return _envelope(*st.gettriggers(
                p.get("session"), p.get("envName")))
        if method == "environment/trigger/rest/addtrigger":
            return _envelope(*st.addtrigger(
                p.get("session"), p.get("envName"), p.get("nodeGroup"),
                p.get("type"), p.get("condition"), p.get("value"),
                p.get("period"), p.get("amount"), p.get("action")))
        if method == "environment/trigger/rest/deletetrigger":
            return _envelope(*st.deletetrigger(
                p.get("session"), p.get("envName"), p.get("id")))

        # Task scheduler (utils/scheduler/rest/*).
        if method == "utils/scheduler/rest/gettasks":
            return _envelope(*st.gettasks(
                p.get("session"), p.get("envName")))
        if method == "utils/scheduler/rest/createenvtask":
            return _envelope(*st.createenvtask(
                p.get("session"), p.get("envName"), p.get("cron"),
                p.get("command")))
        if method == "utils/scheduler/rest/deletetasks":
            return _envelope(*st.deletetasks(
                p.get("session"), p.get("envName"), p.get("ids")))

        # Collaboration (users/collaboration/rest/*).
        if method == "users/collaboration/rest/invitemember":
            return _envelope(*st.invitemember(
                p.get("session"), p.get("envName"), p.get("email"),
                p.get("role")))
        if method == "users/collaboration/rest/getmembers":
            return _envelope(*st.getmembers(
                p.get("session"), p.get("envName")))

        # Billing history (billing/account/rest/*).
        if method == ("billing/account/rest/"
                      "getextendedaccountbillinghistorybyperiod"):
            return _envelope(*st.getextendedaccountbillinghistorybyperiod(
                p.get("session"), p.get("startTime"), p.get("endTime")))

        return _envelope(1, {"error": f"unknown method [{method}]"})

    @app.api_route(API_PREFIX + "{path:path}", methods=["GET", "POST"])
    async def api(request: Request, path: str):
        # VHC/hx-core reads POST parameters from the form channel only: a JSON
        # body yields "invalid parameter [session]" (verified live).
        if request.method == "POST" and "application/json" in \
                request.headers.get("content-type", ""):
            return _envelope(INVALID_PARAMETER,
                             {"error": "invalid parameter [session]"})
        params = await _collect_params(request)
        return dispatch(path.strip("/"), params)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "service": "mock-jelastic"}

    # Mock application endpoint for app_healthcheck: like a deployed app on
    # the environment domain. Tests route the healthcheck fetch seam here.
    @app.get("/site/{env_name}")
    @app.get("/site/{env_name}/{path:path}")
    async def mock_site(env_name: str, path: str = ""):
        st: MockJelastic = app.state.jelastic
        st.advance_all()
        env = st.envs.get(env_name)
        if env is None or env.removed:
            return JSONResponse({"error": "not found"}, status_code=404)
        if env.status != "running":
            return JSONResponse({"error": "environment not running"},
                                status_code=503)
        if not env.app and not env.vcs:
            return JSONResponse({"error": "no application deployed"},
                                status_code=503)
        title = f"{env_name} - deployed app"
        body = (f"<html><head><title>{title}</title></head>"
                f"<body><h1>{title}</h1></body></html>")
        return HTMLResponse(body)

    return app
