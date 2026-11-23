"""Mock Jelastic API semantics tests (REST level, against the in-process mock).

Covers: auth result codes 701/501, session expiry, async status transitions,
deleteenv password + 2212 semantics, topology/scaling, deploy, JPS install,
logs and environment variables. No real network.
"""

from __future__ import annotations

from mock.codes import (
    AUTH_FAILED,
    DELETE_IN_PROGRESS,
    DOMAIN_UNAVAILABLE,
    ENV_NOT_FOUND,
    INVALID_PARAMETER,
    MANIFEST_CORRUPTED,
    NODE_NOT_FOUND,
    OK,
    SESSION_INVALID,
)
from tests.helpers import signin_raw, wait_status_raw

API = "/1.0/"


def create_env_raw(raw, session, name="testenv", engine="java11"):
    response = raw.post(API + "environment/control/rest/createenvironment", json={
        "session": session,
        "env": {"shortdomain": name, "engine": engine, "region": "default"},
        "nodes": [{"nodeType": "cp", "nodeGroup": "cp",
                   "fixedCloudlets": 1, "flexibleCloudlets": 7}],
    })
    body = response.json()
    assert body["result"] == OK, body
    return body


# --- auth ------------------------------------------------------------------


def test_signin_returns_session(raw, state):
    body = raw.post(API + "users/authentication/rest/signin",
                    data={"login": "demo", "password": "demo"}).json()
    assert body["result"] == OK
    assert body["session"].startswith("mock-sess-")


def test_signin_wrong_password_is_701(raw, state):
    body = raw.post(API + "users/authentication/rest/signin",
                    data={"login": "demo", "password": "nope"}).json()
    assert body["result"] == AUTH_FAILED
    assert "authentication failed" in body["error"]


def test_signin_unknown_login_is_701(raw, state):
    body = raw.post(API + "users/authentication/rest/signin",
                    data={"login": "ghost", "password": "demo"}).json()
    assert body["result"] == AUTH_FAILED


def test_missing_session_is_501(raw):
    body = raw.get(API + "environment/control/rest/getenvs").json()
    assert body["result"] == SESSION_INVALID


def test_unknown_session_is_501(raw):
    body = raw.get(API + "environment/control/rest/getenvs",
                   params={"session": "mock-sess-deadbeef"}).json()
    assert body["result"] == SESSION_INVALID


def test_expired_session_is_501(raw, state):
    session = signin_raw(raw, state)
    state.expire_sessions()
    body = raw.get(API + "environment/control/rest/getenvs",
                   params={"session": session}).json()
    assert body["result"] == SESSION_INVALID


def test_signout_invalidates_session(raw, state):
    session = signin_raw(raw, state)
    body = raw.post(API + "users/authentication/rest/signout",
                    params={"session": session}).json()
    assert body["result"] == OK
    body = raw.get(API + "environment/control/rest/getenvs",
                   params={"session": session}).json()
    assert body["result"] == SESSION_INVALID


# --- environments & async transitions --------------------------------------


def test_create_and_list_env(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "web1")
    env = wait_status_raw(raw, session, "web1", "running")
    assert env["domain"] == "web1.mock.jelastic.local"
    assert env["status"] == 1  # real catalog: 1 = running
    listed = raw.get(API + "environment/control/rest/getenvs",
                     params={"session": session}).json()
    assert [e["envName"] for e in listed["infos"]] == ["web1"]


def test_create_env_is_async(raw, state):
    session = signin_raw(raw, state)
    state.config.transition_seconds = 0.5
    create_env_raw(raw, session, "slow")
    body = raw.get(API + "environment/control/rest/getenvinfo",
                   params={"session": session, "envName": "slow"}).json()
    assert body["env"]["statusName"] == "creating"  # fire-and-forget ack
    wait_status_raw(raw, session, "slow", "running")


def test_create_duplicate_name_is_2314(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "dup")
    body = raw.post(API + "environment/control/rest/createenvironment", json={
        "session": session, "env": {"shortdomain": "dup"}}).json()
    assert body["result"] == DOMAIN_UNAVAILABLE


def test_create_invalid_name_is_invalid_param(raw, state):
    session = signin_raw(raw, state)
    body = raw.post(API + "environment/control/rest/createenvironment", json={
        "session": session, "env": {"shortdomain": "Bad Name!"}}).json()
    assert body["result"] == INVALID_PARAMETER


def test_lifecycle_transitions(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "life")
    wait_status_raw(raw, session, "life", "running")

    assert raw.get(API + "environment/control/rest/stopenv",
                   params={"session": session, "envName": "life"}
                   ).json()["result"] == OK
    wait_status_raw(raw, session, "life", "stopped")

    assert raw.get(API + "environment/control/rest/startenv",
                   params={"session": session, "envName": "life"}
                   ).json()["result"] == OK
    wait_status_raw(raw, session, "life", "running")

    assert raw.get(API + "environment/control/rest/sleepenv",
                   params={"session": session, "envName": "life"}
                   ).json()["result"] == OK
    wait_status_raw(raw, session, "life", "sleep")


def test_start_on_running_env_is_idempotent(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "idem")
    wait_status_raw(raw, session, "idem", "running")
    body = raw.get(API + "environment/control/rest/startenv",
                   params={"session": session, "envName": "idem"}).json()
    assert body["result"] == OK


def test_op_on_missing_env_is_env_not_found(raw, state):
    session = signin_raw(raw, state)
    body = raw.get(API + "environment/control/rest/startenv",
                   params={"session": session, "envName": "ghost"}).json()
    assert body["result"] == ENV_NOT_FOUND


# --- deleteenv: password + 2212 semantics ----------------------------------


def test_deleteenv_requires_password(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "delpw")
    wait_status_raw(raw, session, "delpw", "running")
    body = raw.post(API + "environment/control/rest/deleteenv",
                    params={"session": session, "envName": "delpw"}).json()
    assert body["result"] == INVALID_PARAMETER
    assert body["error"] == "invalid parameter [password] for method [DeleteEnv]"


def test_deleteenv_wrong_password_is_701(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "delbad")
    wait_status_raw(raw, session, "delbad", "running")
    body = raw.post(API + "environment/control/rest/deleteenv",
                    params={"session": session, "envName": "delbad",
                            "password": "wrong"}).json()
    assert body["result"] == AUTH_FAILED


def test_deleteenv_and_2212_semantics(raw, state):
    session = signin_raw(raw, state)
    state.config.transition_seconds = 1.0  # long enough for the 2212 window
    create_env_raw(raw, session, "del")
    wait_status_raw(raw, session, "del", "running")

    body = raw.post(API + "environment/control/rest/deleteenv",
                    params={"session": session, "envName": "del",
                            "password": "demo"}).json()
    assert body["result"] == OK

    # retry while deletion is running -> 2212
    body = raw.post(API + "environment/control/rest/deleteenv",
                    params={"session": session, "envName": "del",
                            "password": "demo"}).json()
    assert body["result"] == DELETE_IN_PROGRESS

    # any op against a deleting env -> 2212
    body = raw.get(API + "environment/control/rest/startenv",
                   params={"session": session, "envName": "del"}).json()
    assert body["result"] == DELETE_IN_PROGRESS

    # after the transition the environment is gone
    import time

    deadline_body = {"result": OK}
    deadline = time.monotonic() + 10
    while deadline_body.get("result") == OK and time.monotonic() < deadline:
        time.sleep(0.05)
        deadline_body = raw.get(API + "environment/control/rest/getenvinfo",
                                params={"session": session, "envName": "del"}).json()
    assert deadline_body["result"] == ENV_NOT_FOUND
    listed = raw.get(API + "environment/control/rest/getenvs",
                     params={"session": session}).json()
    assert listed["infos"] == []


def test_deleteenv_missing_env_is_env_not_found(raw, state):
    session = signin_raw(raw, state)
    body = raw.post(API + "environment/control/rest/deleteenv",
                    params={"session": session, "envName": "ghost",
                            "password": "demo"}).json()
    assert body["result"] == ENV_NOT_FOUND


# --- topology & scaling -----------------------------------------------------


def test_changetopology_add_and_remove_nodes(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "topo")
    wait_status_raw(raw, session, "topo", "running")

    body = raw.post(API + "environment/control/rest/changetopology", json={
        "session": session, "envName": "topo",
        "nodes": [{"nodeType": "cp", "nodeGroup": "cp", "count": 2,
                   "fixedCloudlets": 2, "flexibleCloudlets": 8}]}).json()
    assert body["result"] == OK
    env = wait_status_raw(raw, session, "topo", "running")
    assert len(env["nodes"]) == 2

    body = raw.post(API + "environment/control/rest/changetopology", json={
        "session": session, "envName": "topo",
        "nodes": [{"nodeType": "cp", "nodeGroup": "cp", "count": 1}]}).json()
    assert body["result"] == OK
    env = wait_status_raw(raw, session, "topo", "running")
    assert len(env["nodes"]) == 1

    # add a database node group
    body = raw.post(API + "environment/control/rest/changetopology", json={
        "session": session, "envName": "topo",
        "nodes": [{"nodeType": "db", "nodeGroup": "db", "count": 1,
                   "fixedCloudlets": 2, "flexibleCloudlets": 4}]}).json()
    assert body["result"] == OK
    env = wait_status_raw(raw, session, "topo", "running")
    groups = sorted(n["nodeGroup"] for n in env["nodes"])
    assert groups == ["cp", "db"]


def test_setcloudletscountbyid(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "scale")
    env = wait_status_raw(raw, session, "scale", "running")
    node_id = env["nodes"][0]["id"]

    body = raw.post(API + "environment/control/rest/setcloudletscountbyid",
                    params={"session": session, "envName": "scale",
                            "nodeId": node_id, "fixedCloudlets": 4,
                            "flexibleCloudlets": 12}).json()
    assert body["result"] == OK
    env = wait_status_raw(raw, session, "scale", "running")
    node = next(n for n in env["nodes"] if n["id"] == node_id)
    assert (node["fixedCloudlets"], node["flexibleCloudlets"]) == (4, 12)


def test_setcloudlets_unknown_node(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "nonode")
    wait_status_raw(raw, session, "nonode", "running")
    body = raw.post(API + "environment/control/rest/setcloudletscountbyid",
                    params={"session": session, "envName": "nonode",
                            "nodeId": 999999, "fixedCloudlets": 1,
                            "flexibleCloudlets": 1}).json()
    assert body["result"] == NODE_NOT_FOUND


# --- deployment & JPS -------------------------------------------------------


def test_deployarchive_requires_running_env(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "notrun")
    wait_status_raw(raw, session, "notrun", "running")
    assert raw.get(API + "environment/control/rest/stopenv",
                   params={"session": session, "envName": "notrun"}
                   ).json()["result"] == OK
    wait_status_raw(raw, session, "notrun", "stopped")
    body = raw.post(API + "environment/deployment/rest/deployarchive",
                    params={"session": session, "envName": "notrun",
                            "url": "https://example.com/app.war"}).json()
    assert body["result"] != OK  # env is not running


def test_deployarchive_from_url(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "dep")
    wait_status_raw(raw, session, "dep", "running")
    body = raw.post(API + "environment/deployment/rest/deployarchive",
                    params={"session": session, "envName": "dep",
                            "context": "ROOT",
                            "url": "https://example.com/app.war"}).json()
    assert body["result"] == OK
    assert body["context"] == "ROOT"
    env = wait_status_raw(raw, session, "dep", "running")
    assert env["app"]["url"] == "https://example.com/app.war"


def test_deployarchive_without_source_is_invalid(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "nosrc")
    wait_status_raw(raw, session, "nosrc", "running")
    body = raw.post(API + "environment/deployment/rest/deployarchive",
                    params={"session": session, "envName": "nosrc"}).json()
    assert body["result"] == INVALID_PARAMETER


def test_jps_install_inline_manifest(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "jps")
    wait_status_raw(raw, session, "jps", "running")
    body = raw.post(API + "marketplace/jps/rest/install",
                    params={"session": session, "envName": "jps",
                            "jps": "type: install\nname: hello\n"}).json()
    assert body["result"] == OK
    assert "successText" in body
    env = wait_status_raw(raw, session, "jps", "running")
    assert len(env["installations"]) == 1


def test_jps_install_corrupted_manifest_is_11001(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "badjps")
    wait_status_raw(raw, session, "badjps", "running")
    body = raw.post(API + "marketplace/jps/rest/install",
                    params={"session": session, "envName": "badjps",
                            "jps": "type: install\n\tname: tabbed\n"}).json()
    assert body["result"] == MANIFEST_CORRUPTED


def test_jps_install_bad_settings_is_invalid(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "setjps")
    wait_status_raw(raw, session, "setjps", "running")
    body = raw.post(API + "marketplace/jps/rest/install",
                    params={"session": session, "envName": "setjps",
                            "jps": "type: install\nname: x\n",
                            "settings": "{not json"}).json()
    assert body["result"] == INVALID_PARAMETER


# --- logs & variables -------------------------------------------------------


def test_getlogs_records_lifecycle_events(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "logs")
    wait_status_raw(raw, session, "logs", "running")
    body = raw.get(API + "environment/control/rest/getlogs",
                   params={"session": session, "envName": "logs"}).json()
    assert body["result"] == OK
    assert any("creation requested" in line for line in body["logs"])


def test_env_variables_roundtrip(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "vars")
    wait_status_raw(raw, session, "vars", "running")

    body = raw.post(API + "environment/control/rest/setenvvariables", json={
        "session": session, "envName": "vars",
        "vars": {"MODE": "prod", "TIER": "web"}}).json()
    assert body["result"] == OK
    assert body["vars"] == {"MODE": "prod", "TIER": "web"}

    body = raw.get(API + "environment/control/rest/getenvvariables",
                   params={"session": session, "envName": "vars"}).json()
    assert body["vars"] == {"MODE": "prod", "TIER": "web"}

    body = raw.post(API + "environment/control/rest/addcontainerenvvars",
                    params={"session": session, "envName": "vars",
                            "name": "EXTRA", "value": "1"}).json()
    assert body["vars"]["EXTRA"] == "1"

    body = raw.post(API + "environment/control/rest/removecontainerenvvars",
                    params={"session": session, "envName": "vars",
                            "name": "MODE"}).json()
    assert "MODE" not in body["vars"]


# --- misc -------------------------------------------------------------------


def test_unknown_method_returns_result_envelope(raw, state):
    session = signin_raw(raw, state)
    body = raw.get(API + "environment/control/rest/doesnotexist",
                   params={"session": session}).json()
    assert body["result"] != OK
    assert "unknown method" in body["error"]


def test_healthz(raw):
    assert raw.get("/healthz").json()["status"] == "ok"
