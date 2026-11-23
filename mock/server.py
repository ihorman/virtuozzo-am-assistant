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

from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .codes import OK
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
            params[key] = filename if filename is not None else value
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
        if method == "environment/control/rest/getenvstat":
            return _envelope(*st.getenvstat(p.get("session"), p.get("envName")))
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
                p.get("session"), p.get("envName"), p.get("password")))
        if method == "environment/control/rest/changetopology":
            return _envelope(*st.changetopology(
                p.get("session"), p.get("envName"), p.get("nodes")))
        if method == "environment/control/rest/setcloudletscountbyid":
            return _envelope(*st.setcloudletscountbyid(
                p.get("session"), p.get("envName"), p.get("nodeId"),
                p.get("fixedCloudlets"), p.get("flexibleCloudlets")))

        if method in ("environment/deployment/rest/deployarchive",
                      "environment/control/rest/deployapp"):
            return _envelope(*st.deployarchive(
                p.get("session"), p.get("envName"), p.get("context"),
                p.get("url"), p.get("archive"), p.get("nodeGroup")))

        if method == "marketplace/jps/rest/install":
            return _envelope(*st.install_jps(
                p.get("session"), p.get("jps"), p.get("envName"),
                p.get("settings"), p.get("skipNodeEmails")))

        if method == "environment/control/rest/getlogs":
            return _envelope(*st.getlogs(
                p.get("session"), p.get("envName"), p.get("limit")))
        if method in ("environment/control/rest/getenvvariables",
                      "environment/control/rest/getcontainerenvvars"):
            return _envelope(*st.getenvvariables(p.get("session"), p.get("envName")))
        if method in ("environment/control/rest/setenvvariables",
                      "environment/control/rest/setcontainerenvvars"):
            return _envelope(*st.setenvvariables(
                p.get("session"), p.get("envName"), p.get("vars")))
        if method in ("environment/control/rest/addcontainerenvvars",):
            return _envelope(*st.addenvvariable(
                p.get("session"), p.get("envName"), p.get("name"), p.get("value")))
        if method in ("environment/control/rest/removecontainerenvvars",):
            return _envelope(*st.removeenvvariable(
                p.get("session"), p.get("envName"), p.get("name")))

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

        return _envelope(1, {"error": f"unknown method [{method}]"})

    @app.api_route(API_PREFIX + "{path:path}", methods=["GET", "POST"])
    async def api(request: Request, path: str):
        params = await _collect_params(request)
        return dispatch(path.strip("/"), params)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "service": "mock-jelastic"}

    return app
