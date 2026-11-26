"""T3 acceptance suite: MCP server tools, auth passthrough, safety gates.

Everything runs against the in-process mock (ASGI transport, no network —
enforced by the conftest socket guard). Covered per the T3 acceptance:

- pytest green vs the mock,
- MCP handshake + tools/list + tools/call exercised programmatically
  (direct protocol core, Streamable HTTP /mcp, and stdio mode),
- destructive tools without ``confirm=true`` are refused (and mutate
  nothing), ``env_delete`` additionally requires the password.
"""

from __future__ import annotations

import io
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
)
from mcp.server import MCPServer, SERVER_NAME
from mcp.tools import AuthConfig, NoCredentialsError, TOOLS
from tests.helpers import TRANSITION, run, signin_raw, wait_status_raw

# The frozen v1 tool surface from docs/PLAN.md section 2, extended by T12
# with the deploy-anything tools (project_inspect, deploy_vcs,
# app_healthcheck, deploy_any) and by T13 with the coverage tools
# (docs/SCENARIOS.md rows 7-17).
PLAN_TOOLS = {
    "envs_list", "env_info", "env_stats", "env_create", "env_start",
    "env_stop", "env_sleep", "env_delete", "node_scale", "topology_change",
    "deploy_archive", "deploy_jps", "db_add", "logs_read", "env_var_get",
    "env_var_set", "ssl_letsencrypt", "marketplace_list", "templates_list",
    "quota_get", "op_result",
    "project_inspect", "deploy_vcs", "app_healthcheck", "deploy_any",
    "domain_bind", "domain_unbind", "env_clone", "env_restart", "node_restart",
    "endpoint_open", "endpoint_close", "firewall_rule_add",
    "firewall_rule_remove", "file_list", "file_read", "file_upload",
    "file_edit", "trigger_list", "trigger_create", "trigger_delete",
    "task_list", "task_create", "task_delete", "env_share", "env_share_list",
    "usage_list",
}

MUTATING_MINIMAL = [
    ("env_create", {"env_name": "mcp-test-x"}),
    ("env_start", {"env_name": "mcp-test-x"}),
    ("env_stop", {"env_name": "mcp-test-x"}),
    ("env_sleep", {"env_name": "mcp-test-x"}),
    ("env_delete", {"env_name": "mcp-test-x", "password": "demo"}),
    ("node_scale", {"env_name": "mcp-test-x", "node_id": 1001,
                    "fixed_cloudlets": 1, "flexible_cloudlets": 3}),
    ("topology_change", {"env_name": "mcp-test-x",
                         "nodes": [{"nodeType": "cp", "nodeGroup": "cp"}]}),
    ("deploy_archive", {"env_name": "mcp-test-x",
                        "url": "https://example.com/app.war"}),
    ("deploy_jps", {"env_name": "mcp-test-x",
                    "jps": "https://example.com/manifest.jps"}),
    ("deploy_vcs", {"env_name": "mcp-test-x",
                    "git_url": "https://github.com/acme/demo"}),
    ("deploy_any", {"env_name": "mcp-test-x",
                    "source_url": "https://github.com/acme/demo"}),
    ("db_add", {"env_name": "mcp-test-x", "engine": "mysql"}),
    ("env_var_set", {"env_name": "mcp-test-x", "vars": {"A": "1"}}),
    ("ssl_letsencrypt", {"env_name": "mcp-test-x",
                         "ext_domain": "app.example.com"}),
    ("domain_bind", {"env_name": "mcp-test-x",
                     "ext_domain": "app.example.com"}),
    ("domain_unbind", {"env_name": "mcp-test-x",
                       "ext_domain": "app.example.com"}),
    ("env_clone", {"env_name": "mcp-test-x", "new_name": "mcp-test-y"}),
    ("env_restart", {"env_name": "mcp-test-x"}),
    ("node_restart", {"env_name": "mcp-test-x", "node_id": 1001}),
    ("endpoint_open", {"env_name": "mcp-test-x", "node_id": 1001,
                       "port": 8080, "protocol": "TCP"}),
    ("endpoint_close", {"env_name": "mcp-test-x", "node_id": 1001,
                        "port": 8080, "protocol": "TCP"}),
    ("firewall_rule_add", {"env_name": "mcp-test-x", "node_id": 1001,
                           "direction": "ACCEPT", "protocol": "TCP",
                           "port": 80, "subnet": "0.0.0.0/0"}),
    ("firewall_rule_remove", {"env_name": "mcp-test-x", "node_id": 1001,
                              "direction": "ACCEPT", "protocol": "TCP",
                              "port": 80, "subnet": "0.0.0.0/0"}),
    ("file_upload", {"env_name": "mcp-test-x", "path": "/tmp/a.txt",
                     "content_b64": "aGVsbG8="}),
    ("file_edit", {"env_name": "mcp-test-x", "path": "/tmp/a.txt",
                   "content": "hello"}),
    ("trigger_create", {"env_name": "mcp-test-x", "metric": "CPU",
                        "scale_up_threshold": 70, "step": 1}),
    ("trigger_delete", {"env_name": "mcp-test-x", "trigger_id": 1}),
    ("task_create", {"env_name": "mcp-test-x", "cron": "0 2 * * *",
                     "action": "restart"}),
    ("task_delete", {"env_name": "mcp-test-x", "task_id": 1}),
    ("env_share", {"env_name": "mcp-test-x",
                   "email": "teammate@example.com", "role": "viewer"}),
]


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def mcp_server(app) -> MCPServer:
    return MCPServer(transport=httpx.ASGITransport(app=app),
                     poll_interval=0.01, timeout=5.0)


@pytest.fixture
def auth(raw, state) -> AuthConfig:
    return AuthConfig(host="https://mock.local",
                      session=signin_raw(raw, state))


def rpc(method, params=None, id=1) -> dict:
    message = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call(mcp_server, auth, name, arguments, id=7):
    """tools/call through the protocol core; returns the MCP result dict."""
    response = run(mcp_server.handle_message(
        rpc("tools/call", {"name": name, "arguments": arguments}, id=id),
        auth=auth))
    assert response["id"] == id
    return response["result"]


def result_payload(result) -> dict:
    """Decode the JSON payload out of the text content block."""
    assert result["content"][0]["type"] == "text"
    return json.loads(result["content"][0]["text"])


def snapshot(state) -> dict:
    """Observable platform state, for proving refused calls mutate nothing."""
    return {name: (env.status, tuple(n.id for n in env.nodes),
                   dict(env.env_vars), env.app and dict(env.app),
                   env.ssl and dict(env.ssl),
                   tuple(d["extDomain"] for d in env.ext_domains),
                   tuple((e["id"], e["nodeId"], e["privatePort"])
                         for e in env.endpoints),
                   tuple((r["id"], r["nodeId"], r["ports"], r["src"],
                          r["action"]) for r in env.firewall_rules),
                   dict(env.files),
                   tuple(t["id"] for t in env.triggers),
                   tuple(t["id"] for t in env.tasks),
                   tuple(s["email"] for s in env.shares))
            for name, env in state.envs.items()}


def make_env(raw, session, name="mcp-test-env"):
    """Create + settle an environment straight over REST (live wire shape:
    form-encoded POST with env/nodes as JSON strings)."""
    response = raw.post("/1.0/environment/control/rest/createenvironment",
                        data={"session": session,
                              "env": json.dumps({"shortdomain": name}),
                              "nodes": json.dumps([
                                  {"nodeType": "cp", "nodeGroup": "cp",
                                   "fixedCloudlets": 1,
                                   "flexibleCloudlets": 7}])})
    assert response.json()["result"] == 0
    wait_status_raw(raw, session, name, "running")
    return name


# --- handshake / protocol core ---------------------------------------------


def test_initialize_handshake_and_version_negotiation(mcp_server, auth):
    response = run(mcp_server.handle_message(rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0.0.1"},
    }), auth=auth))
    result = response["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == SERVER_NAME
    assert result["instructions"]

    # An older supported revision is echoed back...
    response = run(mcp_server.handle_message(rpc("initialize", {
        "protocolVersion": "2024-11-05"}), auth=auth))
    assert response["result"]["protocolVersion"] == "2024-11-05"
    # ...and an unknown revision is answered with the server's latest.
    response = run(mcp_server.handle_message(rpc("initialize", {
        "protocolVersion": "1999-01-01"}), auth=auth))
    assert response["result"]["protocolVersion"] == "2025-06-18"


def test_initialized_notification_has_no_response(mcp_server, auth):
    response = run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        auth=auth))
    assert response is None


def test_ping_and_protocol_errors(mcp_server, auth):
    result = run(mcp_server.handle_message(rpc("ping"), auth=auth))
    assert result["result"] == {}

    missing = run(mcp_server.handle_message({"id": 2}, auth=auth))
    assert missing["error"]["code"] == INVALID_REQUEST

    unknown = run(mcp_server.handle_message(rpc("resources/list"), auth=auth))
    assert unknown["error"]["code"] == METHOD_NOT_FOUND

    # A client response (we never send requests) is acknowledged and ignored.
    assert run(mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 9, "result": {}}, auth=auth)) is None

    bad_tool = run(mcp_server.handle_message(
        rpc("tools/call", {"name": "nope"}), auth=auth))
    assert bad_tool["error"]["code"] == INVALID_PARAMS


def test_batch_messages(mcp_server, auth):
    responses = run(mcp_server.handle_payload(
        [rpc("initialize", {"protocolVersion": "2025-06-18"}, id=1),
         rpc("ping", id=2)], auth=auth))
    assert [r["id"] for r in responses] == [1, 2]


# --- tool surface -----------------------------------------------------------


def test_tools_list_matches_plan(mcp_server, auth):
    result = run(mcp_server.handle_message(rpc("tools/list"), auth=auth))
    tools = result["result"]["tools"]
    assert {t["name"] for t in tools} == PLAN_TOOLS == set(TOOLS)
    for tool in tools:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"
        assert "properties" in tool["inputSchema"]


def test_safety_gates_in_schemas():
    for name, spec in TOOLS.items():
        schema = spec.input_schema
        if spec.mutating:
            assert "confirm" in schema["required"], name
            assert schema["properties"]["confirm"]["type"] == "boolean"
        else:
            assert "confirm" not in schema.get("required", []), name
        assert schema["additionalProperties"] is False
    delete = TOOLS["env_delete"].input_schema
    assert "password" in delete["required"]


def test_tool_annotations(mcp_server, auth):
    result = run(mcp_server.handle_message(rpc("tools/list"), auth=auth))
    by_name = {t["name"]: t for t in result["result"]["tools"]}
    assert by_name["envs_list"]["annotations"]["readOnlyHint"] is True
    assert by_name["envs_list"]["annotations"]["destructiveHint"] is False
    assert by_name["env_delete"]["annotations"]["destructiveHint"] is True
    assert by_name["env_delete"]["annotations"]["readOnlyHint"] is False


# --- read tools over tools/call ---------------------------------------------


def test_envs_list_and_read_roundtrip(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-read")

    result = call(mcp_server, auth, "envs_list", {})
    assert result["isError"] is False
    payload = result_payload(result)
    assert payload["count"] == 1
    assert payload["environments"][0]["envName"] == "mcp-test-read"

    info = result_payload(call(mcp_server, auth, "env_info",
                               {"env_name": "mcp-test-read"}))
    assert info["env"]["statusName"] == "running"

    stats = result_payload(call(mcp_server, auth, "env_stats",
                                {"env_name": "mcp-test-read"}))
    assert stats["nodes"][0]["cpu"] >= 0

    logs = result_payload(call(mcp_server, auth, "logs_read",
                               {"env_name": "mcp-test-read", "limit": 5}))
    assert logs["count"] >= 1

    variables = result_payload(call(mcp_server, auth, "env_var_get",
                                    {"env_name": "mcp-test-read"}))
    assert variables["vars"] == {}

    categories = result_payload(call(mcp_server, auth, "marketplace_list", {}))
    assert categories["categories"][0]["name"]
    apps = result_payload(call(mcp_server, auth, "marketplace_list",
                               {"category": 2}))
    assert apps["apps"] and all(a["category"] == 2 for a in apps["apps"])

    templates = result_payload(call(mcp_server, auth, "templates_list",
                                    {"node_group": "db"}))
    assert templates["count"] >= 1
    assert all("db" in t["nodeGroups"] for t in templates["templates"])

    quotas = result_payload(call(mcp_server, auth, "quota_get", {}))
    assert quotas["quotas"]["environments"]["usage"] == 1


# --- live-shape coverage (VHC 8.15.1 wire shapes through the tool layer) ----


def test_logs_read_listing_content_and_dirs(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-logs")

    listing = result_payload(call(mcp_server, auth, "logs_read",
                                  {"env_name": "mcp-test-logs"}))
    assert listing["count"] >= 1
    path = listing["logs"][0]["files"][0]["path"]
    assert path.startswith("/var/log/")

    content = result_payload(call(mcp_server, auth, "logs_read",
                                  {"env_name": "mcp-test-logs",
                                   "path": path, "line": 5}))
    assert content["linescount"] >= 1
    assert "creation requested" in content["body"]
    assert "untrusted" in content["body"]  # framed prose (T4)

    dirs = result_payload(call(mcp_server, auth, "logs_read",
                               {"env_name": "mcp-test-logs",
                                "list_dirs": True}))
    assert dirs["count"] >= 1 and dirs["dirs"][0]["items"]


def test_env_var_get_set_container_groups(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-vars")
    got = result_payload(call(mcp_server, auth, "env_var_get",
                              {"env_name": "mcp-test-vars"}))
    assert got["vars"] == {} and got["node_group"] == "cp"
    setr = result_payload(call(mcp_server, auth, "env_var_set", {
        "env_name": "mcp-test-vars", "vars": {"MODE": "test"},
        "node_group": "cp", "confirm": True}))
    assert setr["vars"] == {"MODE": "test"}
    got = result_payload(call(mcp_server, auth, "env_var_get",
                              {"env_name": "mcp-test-vars"}))
    assert got["vars"] == {"MODE": "test"}
    # live wire path: (set|get)containerenvvarsbygroup, NOT setenvvariables
    assert ("environment/control/rest/setcontainerenvvarsbygroup"
            in state.calls)
    assert ("environment/control/rest/getcontainerenvvarsbygroup"
            in state.calls)


def test_topology_change_routes_scaling_to_applynodegroupdata(mcp_server,
                                                              auth, raw,
                                                              state):
    make_env(raw, auth.session, name="mcp-test-route")
    state.calls.clear()
    result = call(mcp_server, auth, "topology_change", {
        "env_name": "mcp-test-route", "confirm": True,
        "nodes": [{"nodeType": "cp", "nodeGroup": "cp", "count": 2,
                   "fixedCloudlets": 2, "flexibleCloudlets": 6}]})
    assert result["isError"] is False
    assert result_payload(result)["scaled_groups"] == ["cp"]
    # scaling existing groups rides applynodegroupdata, never editnodegroup
    assert "environment/control/rest/applynodegroupdata" in state.calls
    assert "environment/control/rest/changetopology" not in state.calls
    info = result_payload(call(mcp_server, auth, "env_info",
                               {"env_name": "mcp-test-route"}))
    assert len(info["env"]["nodes"]) == 2

    # adding a group rides changetopology (the verified add path)
    state.calls.clear()
    result = call(mcp_server, auth, "db_add", {
        "env_name": "mcp-test-route", "engine": "postgres", "confirm": True})
    assert result["isError"] is False
    assert "environment/control/rest/changetopology" in state.calls
    assert "environment/control/rest/applynodegroupdata" not in state.calls


def test_env_create_wizard_mapping(mcp_server, auth):
    result = call(mcp_server, auth, "env_create", {
        "env_name": "mcp-test-create", "stack": "php:8.3",
        "db": "mysql:8.0", "cloudlets": 4, "confirm": True})
    assert result["isError"] is False
    assert result_payload(result)["envName"] == "mcp-test-create"

    info = result_payload(call(mcp_server, auth, "env_info",
                               {"env_name": "mcp-test-create"}))["env"]
    assert info["engine"] == "php:8.3"
    groups = {n["nodeGroup"]: n for n in info["nodes"]}
    assert set(groups) == {"cp", "db"}
    assert groups["cp"]["nodeType"] == "php"
    assert groups["db"]["nodeType"] == "mysql"
    assert (groups["cp"]["fixedCloudlets"],
            groups["cp"]["flexibleCloudlets"]) == (1, 3)


def test_op_result_polling(mcp_server, auth, raw, state):
    session = auth.session
    # Fire-and-forget creation, then poll for running.
    raw.post("/1.0/environment/control/rest/createenvironment",
             data={"session": session,
                   "env": json.dumps({"shortdomain": "mcp-test-op"}),
                   "nodes": json.dumps([{"nodeType": "cp",
                                         "nodeGroup": "cp"}])})
    result = call(mcp_server, auth, "op_result",
                  {"env_name": "mcp-test-op", "op": "status",
                   "target": ["running"], "timeout": 5})
    payload = result_payload(result)
    assert result["isError"] is False
    assert payload["status"] == "done"
    assert payload["observed"]["statusName"] == "running"

    # A deadline hit is NOT an error: in_progress with observed state.
    result = call(mcp_server, auth, "op_result",
                  {"env_name": "mcp-test-op", "op": "status",
                   "target": ["sleep"], "timeout": 0.05})
    payload = result_payload(result)
    assert result["isError"] is False
    assert payload["status"] == "in_progress"
    assert payload["observed"]["statusName"] == "running"

    # Deletion verification: op=gone resolves once the env disappears.
    state.deleteenv(session, "mcp-test-op", state.config.password,
                    "mcp-test-op")  # typed confirmation: passphrase=envName
    result = call(mcp_server, auth, "op_result",
                  {"env_name": "mcp-test-op", "op": "gone", "timeout": 5})
    assert result_payload(result)["status"] == "done"


# --- safety gates ------------------------------------------------------------


@pytest.mark.parametrize("name,arguments", MUTATING_MINIMAL)
def test_mutating_without_confirm_is_refused(mcp_server, auth, state, name,
                                             arguments):
    before = snapshot(state)
    result = call(mcp_server, auth, name, arguments)
    assert result["isError"] is True
    payload = result_payload(result)
    assert payload["error"]["code"] == "confirm_required"
    assert "confirm" in payload["error"]["message"]
    assert snapshot(state) == before  # nothing reached the platform


@pytest.mark.parametrize("bad_confirm", ["true", 1, "yes", None])
def test_confirm_must_be_json_boolean_true(mcp_server, auth, state, bad_confirm):
    before = snapshot(state)
    result = call(mcp_server, auth, "env_stop",
                  {"env_name": "mcp-test-x", "confirm": bad_confirm})
    assert result["isError"] is True
    assert result_payload(result)["error"]["code"] == "confirm_required"
    assert snapshot(state) == before


def test_env_delete_requires_password(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-del")
    before = snapshot(state)
    result = call(mcp_server, auth, "env_delete",
                  {"env_name": "mcp-test-del", "confirm": True})
    assert result["isError"] is True
    assert result_payload(result)["error"]["code"] == "password_required"
    assert snapshot(state) == before


def test_env_delete_wrong_password_refused(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-del2")
    result = call(mcp_server, auth, "env_delete", {
        "env_name": "mcp-test-del2", "password": "wrong-secret",
        "confirm": True})
    assert result["isError"] is True
    payload = result_payload(result)
    assert payload["error"]["code"] == "auth_failed"
    assert "wrong-secret" not in json.dumps(payload)  # never echoed
    assert "mcp-test-del2" in state.envs  # still there


def test_env_delete_success_and_password_never_stored(mcp_server, auth, raw,
                                                      state):
    make_env(raw, auth.session, name="mcp-test-del3")
    result = call(mcp_server, auth, "env_delete", {
        "env_name": "mcp-test-del3", "password": state.config.password,
        "confirm": True})
    assert result["isError"] is False
    payload = result_payload(result)
    assert payload["detail"] == "environment deleted and verified gone"
    assert state.config.password not in json.dumps(payload)
    assert "mcp-test-del3" not in state.envs


def test_confirmed_mutations_work(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-mut")

    result = call(mcp_server, auth, "env_var_set", {
        "env_name": "mcp-test-mut", "vars": {"MODE": "test"}, "confirm": True})
    assert result["isError"] is False
    assert result_payload(result)["vars"] == {"MODE": "test"}

    result = call(mcp_server, auth, "deploy_archive", {
        "env_name": "mcp-test-mut", "url": "https://example.com/a.war",
        "confirm": True})
    assert result["isError"] is False

    result = call(mcp_server, auth, "deploy_jps", {
        "env_name": "mcp-test-mut",
        "jps": "https://example.com/manifest.jps", "confirm": True})
    assert result["isError"] is False

    result = call(mcp_server, auth, "node_scale", {
        "env_name": "mcp-test-mut", "node_id":
            state.envs["mcp-test-mut"].nodes[0].id,
        "fixed_cloudlets": 2, "flexible_cloudlets": 6, "confirm": True})
    assert result["isError"] is False

    result = call(mcp_server, auth, "ssl_letsencrypt", {
        "env_name": "mcp-test-mut", "ext_domain": "app.example.com",
        "confirm": True})
    assert result["isError"] is False
    assert result_payload(result)["issuer"] == "Let's Encrypt"

    # Lifecycle: stop and verify observable state.
    result = call(mcp_server, auth, "env_stop",
                  {"env_name": "mcp-test-mut", "confirm": True})
    assert result["isError"] is False
    result = call(mcp_server, auth, "op_result", {
        "env_name": "mcp-test-mut", "op": "status", "target": ["stopped"],
        "timeout": 5})
    assert result_payload(result)["status"] == "done"


def test_db_add_appends_db_group(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-db")
    result = call(mcp_server, auth, "db_add", {
        "env_name": "mcp-test-db", "engine": "postgres", "version": "16",
        "cloudlets": 4, "confirm": True})
    assert result["isError"] is False
    info = result_payload(call(mcp_server, auth, "env_info",
                               {"env_name": "mcp-test-db"}))["env"]
    groups = {n["nodeGroup"] for n in info["nodes"]}
    assert groups == {"cp", "db"}

    # Idempotent-checked: a second db_add is a clean error, not a crash.
    result = call(mcp_server, auth, "db_add", {
        "env_name": "mcp-test-db", "engine": "mysql", "confirm": True})
    assert result["isError"] is True
    assert "already has a database node group" in \
        result_payload(result)["error"]["message"]


def test_deploy_archive_needs_exactly_one_source(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-dep")
    for args in ({}, {"url": "https://x/y.war", "archive": "y.war"}):
        arguments = {"env_name": "mcp-test-dep", "confirm": True, **args}
        result = call(mcp_server, auth, "deploy_archive", arguments)
        assert result["isError"] is True
        assert result_payload(result)["error"]["code"] == "invalid_parameter"


def test_ssl_letsencrypt_bad_domain(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-ssl")
    result = call(mcp_server, auth, "ssl_letsencrypt", {
        "env_name": "mcp-test-ssl", "ext_domain": "not a domain",
        "confirm": True})
    assert result["isError"] is True
    assert result_payload(result)["error"]["code"] == "invalid_parameter"


# --- auth passthrough --------------------------------------------------------


def test_no_credentials_is_in_band_error(mcp_server, auth):
    result = call(mcp_server, None, "envs_list", {})
    assert result["isError"] is True
    assert result_payload(result)["error"]["code"] == "no_credentials"


def test_auth_from_headers_case_insensitive():
    auth = AuthConfig.from_headers({"x-jelastic-host": "https://panel",
                                    "X-JELASTIC-SESSION": "tok"})
    assert auth == AuthConfig(host="https://panel", session="tok")
    with pytest.raises(NoCredentialsError):
        AuthConfig.from_headers({"X-Jelastic-Host": "https://panel"})


def test_auth_from_env():
    auth = AuthConfig.from_env({"JELASTIC_HOST": "https://panel",
                                "JELASTIC_SESSION": "tok"})
    assert auth.host == "https://panel"
    with pytest.raises(NoCredentialsError):
        AuthConfig.from_env({})


def test_session_expired_surfaced(mcp_server, state):
    expired = AuthConfig(host="https://mock.local", session="bogus-token")
    result = call(mcp_server, expired, "envs_list", {})
    assert result["isError"] is True
    assert result_payload(result)["error"]["code"] == "session_expired"

    state.expire_sessions()
    result = call(mcp_server, expired, "envs_list", {})
    assert result["isError"] is True


# --- Streamable HTTP /mcp ---------------------------------------------------


@pytest.fixture
def http_client(mcp_server):
    return TestClient(mcp_server.create_app())


def test_http_handshake_and_roundtrip(http_client, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-http")
    headers = {"X-Jelastic-Host": auth.host,
               "X-Jelastic-Session": auth.session}

    response = http_client.post("/mcp", json=rpc("initialize", {
        "protocolVersion": "2025-06-18"}, id=1), headers=headers)
    assert response.status_code == 200
    assert response.json()["result"]["serverInfo"]["name"] == SERVER_NAME
    assert response.headers.get("mcp-session-id")  # session id echoed

    response = http_client.post("/mcp", json=rpc("tools/list", id=2),
                                headers=headers)
    assert {t["name"] for t in response.json()["result"]["tools"]} == PLAN_TOOLS

    response = http_client.post("/mcp", json=rpc("tools/call", {
        "name": "envs_list", "arguments": {}}, id=3), headers=headers)
    result = response.json()["result"]
    assert result["isError"] is False
    assert result_payload(result)["count"] == 1

    # A notification is acknowledged with 202 and an empty body.
    response = http_client.post("/mcp", json={
        "jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers)
    assert response.status_code == 202
    assert response.content == b""


def test_http_destructive_without_confirm_is_refused(http_client, auth):
    headers = {"X-Jelastic-Host": auth.host,
               "X-Jelastic-Session": auth.session}
    response = http_client.post("/mcp", json=rpc("tools/call", {
        "name": "env_stop",
        "arguments": {"env_name": "mcp-test-x"}}, id=1), headers=headers)
    result = response.json()["result"]
    assert result["isError"] is True
    assert result_payload(result)["error"]["code"] == "confirm_required"


def test_http_missing_headers_no_credentials(http_client):
    response = http_client.post("/mcp", json=rpc("tools/call", {
        "name": "envs_list", "arguments": {}}, id=1))
    assert response.json()["result"]["isError"] is True
    assert result_payload(response.json()["result"])["error"]["code"] == \
        "no_credentials"


def test_http_protocol_edges(http_client, auth):
    headers = {"X-Jelastic-Host": auth.host,
               "X-Jelastic-Session": auth.session}
    # GET / DELETE are 405 (no SSE stream, no session termination).
    assert http_client.get("/mcp", headers=headers).status_code == 405
    assert http_client.delete("/mcp", headers=headers).status_code == 405
    # Invalid JSON body -> HTTP 400 with a JSON-RPC parse error.
    response = http_client.post("/mcp", content=b"{not json",
                                headers=headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == PARSE_ERROR
    # Liveness endpoint exists for deploy/T6.
    assert http_client.get("/healthz").json()["status"] == "ok"


# --- stdio mode --------------------------------------------------------------


def test_stdio_roundtrip(mcp_server, raw, state):
    session = signin_raw(raw, state)
    make_env(raw, session, name="mcp-test-stdio")
    lines = "\n".join([
        json.dumps(rpc("initialize", {"protocolVersion": "2025-06-18"}, id=1)),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps(rpc("tools/call", {"name": "envs_list",
                                      "arguments": {}}, id=2)),
        "{broken json",
    ]) + "\n"
    out = io.StringIO()
    run(mcp_server.run_stdio(
        stdin=io.StringIO(lines), stdout=out,
        env={"JELASTIC_HOST": "https://mock.local",
             "JELASTIC_SESSION": session}))

    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    # initialize + envs_list + parse error; the notification is not answered.
    assert len(responses) == 3
    assert responses[0]["result"]["serverInfo"]["name"] == SERVER_NAME
    assert result_payload(responses[1]["result"])["count"] == 1
    assert responses[2]["error"]["code"] == PARSE_ERROR


def test_stdio_without_env_credentials(mcp_server):
    out = io.StringIO()
    run(mcp_server.run_stdio(
        stdin=io.StringIO(json.dumps(rpc("tools/call", {
            "name": "envs_list", "arguments": {}}, id=1)) + "\n"),
        stdout=out, env={}))
    response = json.loads(out.getvalue())
    assert result_payload(response["result"])["error"]["code"] == \
        "no_credentials"


# --- T13 coverage tools: one test per tool (happy + gated-mutation without
#     confirm + a hostile-input rejection) -----------------------------------


def assert_gated(mcp_server, auth, state, name, arguments):
    """Mutating call without confirm=true is refused and mutates nothing."""
    before = snapshot(state)
    result = call(mcp_server, auth, name, dict(arguments))
    assert result["isError"] is True
    assert result_payload(result)["error"]["code"] == "confirm_required"
    assert snapshot(state) == before


def assert_hostile(mcp_server, auth, state, name, arguments):
    """Hostile input is refused as invalid_parameter before any platform
    call (and mutates nothing)."""
    before = snapshot(state)
    result = call(mcp_server, auth, name, dict(arguments))
    assert result["isError"] is True, (name, arguments, result)
    assert result_payload(result)["error"]["code"] == "invalid_parameter"
    assert snapshot(state) == before


def test_tool_domain_bind(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-bind")
    assert_gated(mcp_server, auth, state, "domain_bind",
                 {"env_name": "mcp-test-bind",
                  "ext_domain": "app.example.com"})
    assert_hostile(mcp_server, auth, state, "domain_bind",
                   {"env_name": "mcp-test-bind",
                    "ext_domain": "not a domain", "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "domain_bind", {
        "env_name": "mcp-test-bind", "ext_domain": "app.example.com",
        "ssl": True, "confirm": True})
    payload = result_payload(result)
    assert result["isError"] is False
    assert payload["bound"] is True
    assert payload["ssl"]["issuer"] == "Let's Encrypt"
    assert "environment/binder/rest/bindextdomain" in state.calls
    assert "environment/binder/rest/bindssl" in state.calls


def test_tool_domain_unbind(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-unbind")
    call(mcp_server, auth, "domain_bind",
         {"env_name": "mcp-test-unbind", "ext_domain": "app.example.com",
          "confirm": True})
    assert_gated(mcp_server, auth, state, "domain_unbind",
                 {"env_name": "mcp-test-unbind",
                  "ext_domain": "app.example.com"})
    assert_hostile(mcp_server, auth, state, "domain_unbind",
                   {"env_name": "mcp-test-unbind", "ext_domain": "x y z",
                    "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "domain_unbind", {
        "env_name": "mcp-test-unbind", "ext_domain": "app.example.com",
        "confirm": True})
    payload = result_payload(result)
    assert result["isError"] is False and payload["bound"] is False
    # exact catalog name (the "unbindextdomain" shorthand does not exist)
    assert "environment/binder/rest/removeextdomain" in state.calls


def test_tool_env_clone(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-clone-src")
    assert_gated(mcp_server, auth, state, "env_clone",
                 {"env_name": "mcp-test-clone-src",
                  "new_name": "mcp-test-clone-dst"})
    assert_hostile(mcp_server, auth, state, "env_clone",
                   {"env_name": "mcp-test-clone-src", "new_name": "BAD NAME",
                    "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "env_clone", {
        "env_name": "mcp-test-clone-src", "new_name": "mcp-test-clone-dst",
        "confirm": True})
    assert result["isError"] is False
    assert result_payload(result)["envName"] == "mcp-test-clone-dst"
    assert "environment/control/rest/cloneenv" in state.calls
    ready = call(mcp_server, auth, "op_result",
                 {"env_name": "mcp-test-clone-dst", "op": "status",
                  "target": ["running"], "timeout": 5})
    assert result_payload(ready)["status"] == "done"


def test_tool_env_restart(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-restart")
    assert_gated(mcp_server, auth, state, "env_restart",
                 {"env_name": "mcp-test-restart"})
    assert_hostile(mcp_server, auth, state, "env_restart",
                   {"env_name": "ENV; rm -rf /", "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "env_restart",
                  {"env_name": "mcp-test-restart", "confirm": True})
    assert result["isError"] is False
    assert "restart" in result_payload(result)["detail"]
    assert "environment/control/rest/restartnodes" in state.calls


def test_tool_node_restart(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-noderestart")
    node_id = state.envs["mcp-test-noderestart"].nodes[0].id
    assert_gated(mcp_server, auth, state, "node_restart",
                 {"env_name": "mcp-test-noderestart", "node_id": node_id})
    assert_hostile(mcp_server, auth, state, "node_restart",
                   {"env_name": "mcp-test-noderestart", "node_id": 0,
                    "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "node_restart", {
        "env_name": "mcp-test-noderestart", "node_id": node_id,
        "confirm": True})
    assert result["isError"] is False
    assert result_payload(result)["node"]["id"] == node_id
    assert "environment/control/rest/restartnodebyid" in state.calls


def test_tool_endpoint_open(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-ep")
    node_id = state.envs["mcp-test-ep"].nodes[0].id
    assert_gated(mcp_server, auth, state, "endpoint_open",
                 {"env_name": "mcp-test-ep", "node_id": node_id,
                  "port": 8080, "protocol": "TCP"})
    assert_hostile(mcp_server, auth, state, "endpoint_open",
                   {"env_name": "mcp-test-ep", "node_id": node_id,
                    "port": 65536, "protocol": "TCP", "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "endpoint_open", {
        "env_name": "mcp-test-ep", "node_id": node_id, "port": 8080,
        "protocol": "TCP", "confirm": True})
    payload = result_payload(result)
    assert result["isError"] is False
    # PUBLIC EXPOSURE warning the agent must surface
    assert "INTERNET EXPOSURE" in payload["warning"]
    assert "ENTIRE INTERNET" in payload["warning"]
    assert payload["endpoint"]["privatePort"] == 8080
    assert "environment/control/rest/addendpoint" in state.calls
    # failure mode: the same port again is a port conflict (mock code 2027)
    result = call(mcp_server, auth, "endpoint_open", {
        "env_name": "mcp-test-ep", "node_id": node_id, "port": 8080,
        "protocol": "TCP", "confirm": True})
    assert result["isError"] is True
    assert result_payload(result)["error"]["code"] == "port_conflict"


def test_tool_endpoint_close(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-epc")
    node_id = state.envs["mcp-test-epc"].nodes[0].id
    call(mcp_server, auth, "endpoint_open", {
        "env_name": "mcp-test-epc", "node_id": node_id, "port": 2222,
        "protocol": "TCP", "confirm": True})
    assert_gated(mcp_server, auth, state, "endpoint_close",
                 {"env_name": "mcp-test-epc", "node_id": node_id,
                  "port": 2222, "protocol": "TCP"})
    assert_hostile(mcp_server, auth, state, "endpoint_close",
                   {"env_name": "mcp-test-epc", "node_id": node_id,
                    "port": 2222, "protocol": "ICMP", "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "endpoint_close", {
        "env_name": "mcp-test-epc", "node_id": node_id, "port": 2222,
        "protocol": "TCP", "confirm": True})
    payload = result_payload(result)
    assert result["isError"] is False
    assert "SERVICE INTERRUPTION" in payload["warning"]
    assert "environment/control/rest/removeendpoint" in state.calls
    assert state.envs["mcp-test-epc"].endpoints == []


def test_tool_firewall_rule_add(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-fw")
    node_id = state.envs["mcp-test-fw"].nodes[0].id
    args = {"env_name": "mcp-test-fw", "node_id": node_id,
            "direction": "ACCEPT", "protocol": "TCP", "port": 80,
            "subnet": "0.0.0.0/0"}
    assert_gated(mcp_server, auth, state, "firewall_rule_add", args)
    assert_hostile(mcp_server, auth, state, "firewall_rule_add",
                   {**args, "subnet": "999.999.999.999/99", "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "firewall_rule_add",
                  {**args, "confirm": True})
    payload = result_payload(result)
    assert result["isError"] is False
    assert "FIREWALL CHANGE" in payload["warning"]
    assert "ENTIRE INTERNET" in payload["warning"]  # 0.0.0.0/0 ACCEPT
    assert payload["rule"]["ports"] == "80"
    assert "environment/security/rest/addrule" in state.calls


def test_tool_firewall_rule_remove(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-fwr")
    node_id = state.envs["mcp-test-fwr"].nodes[0].id
    args = {"env_name": "mcp-test-fwr", "node_id": node_id,
            "direction": "DROP", "protocol": "UDP", "port": 53,
            "subnet": "10.0.0.0/8"}
    call(mcp_server, auth, "firewall_rule_add", {**args, "confirm": True})
    assert_gated(mcp_server, auth, state, "firewall_rule_remove", args)
    assert_hostile(mcp_server, auth, state, "firewall_rule_remove",
                   {**args, "direction": "REJECT", "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "firewall_rule_remove",
                  {**args, "confirm": True})
    payload = result_payload(result)
    assert result["isError"] is False
    assert "FIREWALL CHANGE" in payload["warning"]
    assert "environment/security/rest/removerule" in state.calls
    assert state.envs["mcp-test-fwr"].firewall_rules == []


def test_tool_file_list(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-fl")
    assert_hostile(mcp_server, auth, state, "file_list",
                   {"env_name": "mcp-test-fl",
                    "path": "/var/www/../../etc"})
    state.calls.clear()
    result = call(mcp_server, auth, "file_list",
                  {"env_name": "mcp-test-fl", "path": "/var/www/webroot/ROOT"})
    payload = result_payload(result)
    assert result["isError"] is False
    assert payload["count"] == 1
    assert payload["entries"][0]["name"] == "index.html"
    assert "environment/file/rest/getlist" in state.calls


def test_tool_file_read(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-fr")
    assert_hostile(mcp_server, auth, state, "file_read",
                   {"env_name": "mcp-test-fr",
                    "path": "/var/www/webroot/../../../etc/passwd"})
    state.calls.clear()
    result = call(mcp_server, auth, "file_read", {
        "env_name": "mcp-test-fr",
        "path": "/var/www/webroot/ROOT/index.html"})
    payload = result_payload(result)
    assert result["isError"] is False
    assert payload["truncated"] is False
    assert "hello from mcp-test-fr" in payload["body"]
    assert "untrusted" in payload["body"]  # framed prose (T4)
    assert "environment/file/rest/read" in state.calls


def test_tool_file_upload(mcp_server, auth, raw, state):
    import base64
    make_env(raw, auth.session, name="mcp-test-fu")
    secret_text = "top-secret-file-content-42"
    encoded = base64.b64encode(secret_text.encode()).decode()
    args = {"env_name": "mcp-test-fu", "path": "/var/www/webroot/ROOT/a.txt",
            "content_b64": encoded}
    assert_gated(mcp_server, auth, state, "file_upload", args)
    assert_hostile(mcp_server, auth, state, "file_upload",
                   {**args, "content_b64": "not base64!!", "confirm": True})
    assert_hostile(mcp_server, auth, state, "file_upload",
                   {"env_name": "mcp-test-fu",
                    "path": "/var/www/webroot/../../a.txt",
                    "content_b64": encoded, "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "file_upload", {**args, "confirm": True})
    payload = result_payload(result)
    assert result["isError"] is False
    # content is masked like secrets and NEVER echoed back
    assert payload["content"] == "***"
    assert secret_text not in json.dumps(payload)
    assert encoded not in json.dumps(payload)
    assert "environment/file/rest/upload" in state.calls
    stored = state.envs["mcp-test-fu"].files[args["path"]]
    assert stored == secret_text


def test_tool_file_edit(mcp_server, auth, raw, state):
    import base64
    make_env(raw, auth.session, name="mcp-test-fe")
    path = "/var/www/webroot/ROOT/index.html"
    assert_gated(mcp_server, auth, state, "file_edit",
                 {"env_name": "mcp-test-fe", "path": path,
                  "content": "new"})
    assert_hostile(mcp_server, auth, state, "file_edit",
                   {"env_name": "mcp-test-fe", "path": path,
                    "find": "a", "replace": "b", "content": "x",
                    "confirm": True})
    assert_hostile(mcp_server, auth, state, "file_edit",
                   {"env_name": "mcp-test-fe", "path": path,
                    "confirm": True})  # no edit mode at all
    state.calls.clear()
    result = call(mcp_server, auth, "file_edit", {
        "env_name": "mcp-test-fe", "path": path,
        "find": "hello", "replace": "goodbye", "confirm": True})
    payload = result_payload(result)
    assert result["isError"] is False
    assert payload["mode"] == "replace" and payload["replacements"] == 1
    assert "environment/file/rest/replaceinbody" in state.calls
    secret_text = "full-replacement-body-99"
    state.calls.clear()
    result = call(mcp_server, auth, "file_edit", {
        "env_name": "mcp-test-fe", "path": path,
        "content": secret_text, "confirm": True})
    payload = result_payload(result)
    assert result["isError"] is False
    assert payload["mode"] == "write"
    assert payload["content"] == "***"
    assert secret_text not in json.dumps(payload)  # never echoed
    assert "environment/file/rest/write" in state.calls
    assert state.envs["mcp-test-fe"].files[path] == secret_text


def test_tool_trigger_list(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-tl")
    assert_hostile(mcp_server, auth, state, "trigger_list",
                   {"env_name": "nope; drop table"})
    state.calls.clear()
    result = call(mcp_server, auth, "trigger_list",
                  {"env_name": "mcp-test-tl"})
    payload = result_payload(result)
    assert result["isError"] is False and payload["count"] == 0
    assert "environment/trigger/rest/gettriggers" in state.calls


def test_tool_trigger_create(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-tc")
    args = {"env_name": "mcp-test-tc", "node_group": "cp", "metric": "CPU",
            "scale_up_threshold": 70, "scale_down_threshold": 20,
            "period": 5, "step": 1}
    assert_gated(mcp_server, auth, state, "trigger_create", args)
    assert_hostile(mcp_server, auth, state, "trigger_create",
                   {**args, "metric": "LOAD", "confirm": True})
    assert_hostile(mcp_server, auth, state, "trigger_create",
                   {**args, "scale_down_threshold": 80, "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "trigger_create", {**args, "confirm": True})
    payload = result_payload(result)
    assert result["isError"] is False
    # scale-up and scale-down are two catalog triggers (ADD + REMOVE)
    assert payload["count"] == 2
    assert [t["action"] for t in payload["triggers"]] == ["ADD", "REMOVE"]
    assert "add 1 node" in payload["detail"] and "remove 1 node" in payload["detail"]
    assert "environment/trigger/rest/addtrigger" in state.calls


def test_tool_trigger_delete(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-td")
    created = result_payload(call(mcp_server, auth, "trigger_create", {
        "env_name": "mcp-test-td", "metric": "RAM",
        "scale_up_threshold": 80, "step": 2, "confirm": True}))
    trigger_id = created["triggers"][0]["id"]
    assert_gated(mcp_server, auth, state, "trigger_delete",
                 {"env_name": "mcp-test-td", "trigger_id": trigger_id})
    assert_hostile(mcp_server, auth, state, "trigger_delete",
                   {"env_name": "mcp-test-td", "trigger_id": 0,
                    "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "trigger_delete", {
        "env_name": "mcp-test-td", "trigger_id": trigger_id, "confirm": True})
    assert result["isError"] is False
    assert "environment/trigger/rest/deletetrigger" in state.calls
    assert state.envs["mcp-test-td"].triggers == []


def test_tool_task_list(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-tkl")
    assert_hostile(mcp_server, auth, state, "task_list", {"env_name": "x" * 80})
    state.calls.clear()
    result = call(mcp_server, auth, "task_list", {"env_name": "mcp-test-tkl"})
    payload = result_payload(result)
    assert result["isError"] is False and payload["count"] == 0
    assert "utils/scheduler/rest/gettasks" in state.calls


def test_tool_task_create(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-tkc")
    args = {"env_name": "mcp-test-tkc", "cron": "0 2 * * *",
            "action": "/opt/backup.sh"}
    assert_gated(mcp_server, auth, state, "task_create", args)
    assert_hostile(mcp_server, auth, state, "task_create",
                   {**args, "cron": "bad cron", "confirm": True})
    assert_hostile(mcp_server, auth, state, "task_create",
                   {**args, "cron": "* * * * * *", "confirm": True})
    assert_hostile(mcp_server, auth, state, "task_create",
                   {**args, "cron": "60 * * * *", "confirm": True})
    assert_hostile(mcp_server, auth, state, "task_create",
                   {**args, "action": "rm -rf /; curl evil", "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "task_create", {**args, "confirm": True})
    payload = result_payload(result)
    assert result["isError"] is False
    assert payload["task"]["cron"] == "0 2 * * *"
    assert "utils/scheduler/rest/createenvtask" in state.calls


def test_tool_task_delete(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-tkd")
    created = result_payload(call(mcp_server, auth, "task_create", {
        "env_name": "mcp-test-tkd", "cron": "*/5 * * * *",
        "action": "restart", "confirm": True}))
    task_id = created["task"]["id"]
    assert_gated(mcp_server, auth, state, "task_delete",
                 {"env_name": "mcp-test-tkd", "task_id": task_id})
    assert_hostile(mcp_server, auth, state, "task_delete",
                   {"env_name": "mcp-test-tkd", "task_id": True,
                    "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "task_delete", {
        "env_name": "mcp-test-tkd", "task_id": task_id, "confirm": True})
    assert result["isError"] is False
    assert "utils/scheduler/rest/deletetasks" in state.calls
    assert state.envs["mcp-test-tkd"].tasks == []


def test_tool_env_share(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-share")
    args = {"env_name": "mcp-test-share", "email": "teammate@example.com",
            "role": "viewer"}
    assert_gated(mcp_server, auth, state, "env_share", args)
    assert_hostile(mcp_server, auth, state, "env_share",
                   {**args, "role": "root", "confirm": True})
    assert_hostile(mcp_server, auth, state, "env_share",
                   {**args, "email": "not-an-email", "confirm": True})
    state.calls.clear()
    result = call(mcp_server, auth, "env_share", {**args, "confirm": True})
    payload = result_payload(result)
    assert result["isError"] is False
    assert payload["member"]["role"] == "viewer"
    assert "users/collaboration/rest/invitemember" in state.calls


def test_tool_env_share_list(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-sharelist")
    call(mcp_server, auth, "env_share", {
        "env_name": "mcp-test-sharelist", "email": "dev@example.com",
        "role": "user", "confirm": True})
    assert_hostile(mcp_server, auth, state, "env_share_list",
                   {"env_name": "../../etc"})
    state.calls.clear()
    result = call(mcp_server, auth, "env_share_list",
                  {"env_name": "mcp-test-sharelist"})
    payload = result_payload(result)
    assert result["isError"] is False and payload["count"] == 1
    assert payload["members"][0]["email"] == "dev@example.com"
    assert "users/collaboration/rest/getmembers" in state.calls


def test_tool_usage_list(mcp_server, auth, raw, state):
    make_env(raw, auth.session, name="mcp-test-usage")
    assert_hostile(mcp_server, auth, state, "usage_list",
                   {"env_name": "mcp-test-usage",
                    "start_time": "yesterday"})
    state.calls.clear()
    result = call(mcp_server, auth, "usage_list",
                  {"env_name": "mcp-test-usage"})
    payload = result_payload(result)
    assert result["isError"] is False
    assert payload["count"] == 1
    assert payload["rows"][0]["envName"] == "mcp-test-usage"
    assert payload["total_cost"] > 0
    assert ("billing/account/rest/"
            "getextendedaccountbillinghistorybyperiod") in state.calls
    # filter to an env with no rows
    empty = result_payload(call(mcp_server, auth, "usage_list",
                                {"env_name": "ghost-env"}))
    assert empty["count"] == 0 and empty["total_cost"] == 0
