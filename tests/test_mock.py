"""Mock Jelastic API semantics tests (REST level, against the in-process mock).

The mock emulates the VHC 8.15.1 wire shapes verified live (T7/T11):
form-encoded POST only (JSON body -> "invalid parameter [session]"), nested
params (env/nodes/vars/data) as JSON strings in form fields, deployarchive
fileUrl+fileName, changetopology env descriptor, deleteenv
passphrase=<envName>, getsumstat, getlogs/readlog/getlogslist per node,
container env vars, numeric status without statusName, result codes
3/11/724. No real network.
"""

from __future__ import annotations

import json

from mock.codes import (
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
    STATUS_CREATING,
)
from tests.helpers import signin_raw, wait_status_raw

API = "/1.0/"

def create_env_raw(raw, session, name="testenv", engine="java11"):
    # Live wire: form-encoded POST; env/nodes travel as JSON strings.
    response = raw.post(API + "environment/control/rest/createenvironment",
                        data={
                            "session": session,
                            "env": json.dumps({"shortdomain": name,
                                               "engine": engine}),
                            "nodes": json.dumps([
                                {"nodeType": "cp", "nodeGroup": "cp",
                                 "fixedCloudlets": 1, "flexibleCloudlets": 7},
                            ]),
                        })
    body = response.json()
    assert body["result"] == OK, body
    return body


def change_topology_raw(raw, session, env_name, nodes):
    return raw.post(API + "environment/control/rest/changetopology", data={
        "session": session, "envName": env_name,
        "env": json.dumps({"region": "", "sslstate": False}),
        "nodes": json.dumps(nodes)}).json()


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

# --- request channel (verified live: form-encoded POST only) ---------------

def test_json_body_is_rejected_like_live(raw, state):
    session = signin_raw(raw, state)
    body = raw.post(API + "environment/control/rest/createenvironment",
                    json={"session": session,
                          "env": {"shortdomain": "jsonbody"}}).json()
    assert body["result"] == INVALID_PARAMETER
    assert body["error"] == "invalid parameter [session]"

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


def test_getenvinfo_is_flat_with_numeric_status(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "flat")
    wait_status_raw(raw, session, "flat", "running")
    body = raw.get(API + "environment/control/rest/getenvinfo",
                   params={"session": session, "envName": "flat"}).json()
    # Live shape (VHC 8.15.1): env/nodes/nodeGroups flat, numeric status,
    # NO statusName.
    assert body["result"] == OK
    assert body["env"]["status"] == 1
    assert "statusName" not in body["env"]
    assert body["nodes"] and body["nodeGroups"]


def test_create_env_is_async(raw, state):
    session = signin_raw(raw, state)
    state.config.transition_seconds = 0.5
    create_env_raw(raw, session, "slow")
    body = raw.get(API + "environment/control/rest/getenvinfo",
                   params={"session": session, "envName": "slow"}).json()
    assert body["env"]["status"] == STATUS_CREATING  # fire-and-forget ack
    wait_status_raw(raw, session, "slow", "running")


def test_create_duplicate_name_is_2314(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "dup")
    body = raw.post(API + "environment/control/rest/createenvironment", data={
        "session": session,
        "env": json.dumps({"shortdomain": "dup"})}).json()
    assert body["result"] == DOMAIN_UNAVAILABLE


def test_create_invalid_name_is_invalid_param(raw, state):
    session = signin_raw(raw, state)
    body = raw.post(API + "environment/control/rest/createenvironment", data={
        "session": session,
        "env": json.dumps({"shortdomain": "Bad Name!"})}).json()
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
    assert body["result"] == ENV_NOT_FOUND == 11  # live: ghost env -> 11

# --- deleteenv: passphrase + password + 2212 semantics ---------------------

def test_deleteenv_requires_passphrase_is_724(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "delconf")
    wait_status_raw(raw, session, "delconf", "running")
    body = raw.post(API + "environment/control/rest/deleteenv", data={
        "session": session, "envName": "delconf",
        "password": "demo"}).json()
    assert body["result"] == CONFIRMATION_FAILED == 724
    assert body["error"] == "Confirmation failed"


def test_deleteenv_requires_password(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "delpw")
    wait_status_raw(raw, session, "delpw", "running")
    body = raw.post(API + "environment/control/rest/deleteenv", data={
        "session": session, "envName": "delpw",
        "passphrase": "delpw"}).json()
    assert body["result"] == INVALID_PARAMETER
    assert body["error"] == "invalid parameter [password] for method [DeleteEnv]"


def test_deleteenv_wrong_password_is_701(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "delbad")
    wait_status_raw(raw, session, "delbad", "running")
    body = raw.post(API + "environment/control/rest/deleteenv", data={
        "session": session, "envName": "delbad",
        "passphrase": "delbad", "password": "wrong"}).json()
    assert body["result"] == AUTH_FAILED


def test_deleteenv_and_2212_semantics(raw, state):
    session = signin_raw(raw, state)
    state.config.transition_seconds = 1.0  # long enough for the 2212 window
    create_env_raw(raw, session, "del")
    wait_status_raw(raw, session, "del", "running")

    body = raw.post(API + "environment/control/rest/deleteenv", data={
        "session": session, "envName": "del",
        "passphrase": "del", "password": "demo"}).json()
    assert body["result"] == OK

    # retry while deletion is running -> 2212
    body = raw.post(API + "environment/control/rest/deleteenv", data={
        "session": session, "envName": "del",
        "passphrase": "del", "password": "demo"}).json()
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
                                params={"session": session,
                                        "envName": "del"}).json()
    assert deadline_body["result"] == ENV_NOT_FOUND
    listed = raw.get(API + "environment/control/rest/getenvs",
                     params={"session": session}).json()
    assert listed["infos"] == []


def test_deleteenv_missing_env_is_env_not_found(raw, state):
    session = signin_raw(raw, state)
    body = raw.post(API + "environment/control/rest/deleteenv", data={
        "session": session, "envName": "ghost",
        "passphrase": "ghost", "password": "demo"}).json()
    assert body["result"] == ENV_NOT_FOUND

# --- topology & scaling -----------------------------------------------------

def test_changetopology_requires_env_descriptor(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "noenv")
    wait_status_raw(raw, session, "noenv", "running")
    # Verified live: a bare envName yields "invalid parameter [env]".
    body = raw.post(API + "environment/control/rest/changetopology", data={
        "session": session, "envName": "noenv",
        "nodes": json.dumps([{"nodeType": "cp", "nodeGroup": "cp",
                              "count": 1}])}).json()
    assert body["result"] == INVALID_PARAMETER
    assert body["error"] == "invalid parameter [env] for method [ChangeTopology]"


def test_changetopology_add_and_remove_nodes(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "topo")
    wait_status_raw(raw, session, "topo", "running")

    body = change_topology_raw(raw, session, "topo", [
        {"nodeType": "cp", "nodeGroup": "cp", "count": 2,
         "fixedCloudlets": 2, "flexibleCloudlets": 8}])
    assert body["result"] == OK
    env = wait_status_raw(raw, session, "topo", "running")
    assert len(env["nodes"]) == 2

    body = change_topology_raw(raw, session, "topo", [
        {"nodeType": "cp", "nodeGroup": "cp", "count": 1}])
    assert body["result"] == OK
    env = wait_status_raw(raw, session, "topo", "running")
    assert len(env["nodes"]) == 1

    # add a database node group (the verified changetopology add path)
    body = change_topology_raw(raw, session, "topo", [
        {"nodeType": "db", "nodeGroup": "db", "count": 1,
         "fixedCloudlets": 2, "flexibleCloudlets": 4}])
    assert body["result"] == OK
    env = wait_status_raw(raw, session, "topo", "running")
    groups = sorted(n["nodeGroup"] for n in env["nodes"])
    assert groups == ["cp", "db"]


def test_applynodegroupdata_scales_existing_group(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "scalegrp")
    wait_status_raw(raw, session, "scalegrp", "running")
    # Verified live shape: nodeGroup + data={"count","fixedCloudlets",
    # "flexibleCloudlets"} (JSON string in the form field).
    body = raw.post(API + "environment/control/rest/applynodegroupdata",
                    data={"session": session, "envName": "scalegrp",
                          "nodeGroup": "cp",
                          "data": json.dumps({"count": 2, "fixedCloudlets": 2,
                                              "flexibleCloudlets": 6})}).json()
    assert body["result"] == OK
    env = wait_status_raw(raw, session, "scalegrp", "running")
    assert len(env["nodes"]) == 2
    assert all((n["fixedCloudlets"], n["flexibleCloudlets"]) == (2, 6)
               for n in env["nodes"])
    # existing groups only: an unknown group is an invalid parameter
    body = raw.post(API + "environment/control/rest/applynodegroupdata",
                    data={"session": session, "envName": "scalegrp",
                          "nodeGroup": "db",
                          "data": json.dumps({"count": 1})}).json()
    assert body["result"] == INVALID_PARAMETER


def test_setcloudletscountbyid(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "scale")
    env = wait_status_raw(raw, session, "scale", "running")
    node_id = env["nodes"][0]["id"]

    body = raw.post(API + "environment/control/rest/setcloudletscountbyid",
                    data={"session": session, "envName": "scale",
                          "nodeId": str(node_id), "fixedCloudlets": "4",
                          "flexibleCloudlets": "12"}).json()
    assert body["result"] == OK
    env = wait_status_raw(raw, session, "scale", "running")
    node = next(n for n in env["nodes"] if n["id"] == node_id)
    assert (node["fixedCloudlets"], node["flexibleCloudlets"]) == (4, 12)


def test_setcloudlets_unknown_node(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "nonode")
    wait_status_raw(raw, session, "nonode", "running")
    body = raw.post(API + "environment/control/rest/setcloudletscountbyid",
                    data={"session": session, "envName": "nonode",
                          "nodeId": "999999", "fixedCloudlets": "1",
                          "flexibleCloudlets": "1"}).json()
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
                    data={"session": session, "envName": "notrun",
                          "fileUrl": "https://example.com/app.war",
                          "fileName": "app.war"}).json()
    assert body["result"] != OK  # env is not running


def test_deployarchive_from_fileurl(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "dep")
    wait_status_raw(raw, session, "dep", "running")
    body = raw.post(API + "environment/deployment/rest/deployarchive",
                    data={"session": session, "envName": "dep",
                          "context": "ROOT",
                          "fileUrl": "https://example.com/app.war",
                          "fileName": "app.war"}).json()
    assert body["result"] == OK
    assert body["context"] == "ROOT"
    env = wait_status_raw(raw, session, "dep", "running")
    assert env["app"]["fileUrl"] == "https://example.com/app.war"


def test_deployarchive_url_param_is_rejected_like_live(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "depurl")
    wait_status_raw(raw, session, "depurl", "running")
    # Verified live: deployarchive wants fileUrl+fileName (NOT url/archive).
    body = raw.post(API + "environment/deployment/rest/deployarchive",
                    data={"session": session, "envName": "depurl",
                          "url": "https://example.com/app.war",
                          "archive": "app.war"}).json()
    assert body["result"] == INVALID_PARAMETER
    assert "fileUrl" in body["error"]


def test_deployarchive_without_source_is_invalid(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "nosrc")
    wait_status_raw(raw, session, "nosrc", "running")
    body = raw.post(API + "environment/deployment/rest/deployarchive",
                    data={"session": session, "envName": "nosrc"}).json()
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

# --- stats & logs (live shapes) ---------------------------------------------

def test_getsumstat_live_shape(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "sumstat")
    wait_status_raw(raw, session, "sumstat", "running")
    body = raw.get(API + "environment/control/rest/getsumstat",
                   params={"session": session, "envName": "sumstat"}).json()
    assert body["result"] == OK
    assert body["appid"] and body["status"] == 1
    nodeids = [s["nodeid"] for s in body["stats"]]
    assert nodeids[-1] == "sum"  # aggregate row is present live
    assert all({"cpu", "mem", "disk"} <= set(s) for s in body["stats"])
    # the old method name does not exist live
    body = raw.get(API + "environment/control/rest/getenvstat",
                   params={"session": session, "envName": "sumstat"}).json()
    assert body["result"] != OK and "unknown method" in body["error"]


def test_getlogs_requires_nodeid_and_lists_files(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "logs")
    env = wait_status_raw(raw, session, "logs", "running")
    node_id = env["nodes"][0]["id"]

    # Verified live: getlogs without nodeId -> result 3.
    body = raw.get(API + "environment/control/rest/getlogs",
                   params={"session": session, "envName": "logs"}).json()
    assert body["result"] == INVALID_PARAMETER
    assert body["error"] == "Parameter 'nodeid' is required."

    body = raw.get(API + "environment/control/rest/getlogs",
                   params={"session": session, "envName": "logs",
                           "nodeId": node_id}).json()
    assert body["result"] == OK
    assert body["keywords"][0]["files"][0]["path"].endswith(".log")


def test_readlog_returns_linescount_and_body(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "readlog")
    env = wait_status_raw(raw, session, "readlog", "running")
    node_id = env["nodes"][0]["id"]

    body = raw.get(API + "environment/control/rest/readlog",
                   params={"session": session, "envName": "readlog",
                           "nodeId": node_id,
                           "path": "/var/log/readlog.log",
                           "line": 5}).json()
    assert body["result"] == OK
    assert set(body) >= {"linescount", "body"}  # verified live shape
    assert "creation requested" in body["body"]
    assert body["linescount"] == body["body"].count("\n")


def test_getlogslist_returns_dirs_and_groups(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "dirs")
    env = wait_status_raw(raw, session, "dirs", "running")
    node_id = env["nodes"][0]["id"]
    body = raw.get(API + "environment/control/rest/getlogslist",
                   params={"session": session, "envName": "dirs",
                           "nodeId": node_id}).json()
    assert body["result"] == OK
    assert body["array"][0]["items"]

# --- container environment variables (live shapes) --------------------------

def test_env_variables_roundtrip(raw, state):
    session = signin_raw(raw, state)
    create_env_raw(raw, session, "vars")
    env = wait_status_raw(raw, session, "vars", "running")
    node_id = env["nodes"][0]["id"]

    # Verified live: setcontainerenvvarsbygroup with form data={"vars":{...}}.
    body = raw.post(
        API + "environment/control/rest/setcontainerenvvarsbygroup",
        data={"session": session, "envName": "vars", "nodeGroup": "cp",
              "data": json.dumps({"vars": {"MODE": "prod", "TIER": "web"}})}
    ).json()
    assert body["result"] == OK
    assert body["object"] == {"MODE": "prod", "TIER": "web"}

    body = raw.get(API + "environment/control/rest/getcontainerenvvarsbygroup",
                   params={"session": session, "envName": "vars",
                           "nodeGroup": "cp"}).json()
    assert body["object"] == {"MODE": "prod", "TIER": "web"}

    body = raw.get(API + "environment/control/rest/getcontainerenvvars",
                   params={"session": session, "envName": "vars",
                           "nodeId": node_id}).json()
    assert body["object"] == {"MODE": "prod", "TIER": "web"}

    # Verified live: addcontainerenvvars with form vars={"K":"V"}.
    body = raw.post(API + "environment/control/rest/addcontainerenvvars",
                    data={"session": session, "envName": "vars",
                          "nodeGroup": "cp",
                          "vars": json.dumps({"EXTRA": "1"})}).json()
    assert body["object"]["EXTRA"] == "1"

    body = raw.post(API + "environment/control/rest/removecontainerenvvars",
                    data={"session": session, "envName": "vars",
                          "nodeGroup": "cp", "name": "MODE"}).json()
    assert "MODE" not in body["object"]

    # The old env-level methods do not exist live.
    for method in ("getenvvariables", "setenvvariables"):
        body = raw.get(API + f"environment/control/rest/{method}",
                       params={"session": session, "envName": "vars"}).json()
        assert body["result"] != OK and "unknown method" in body["error"]

# --- misc -------------------------------------------------------------------

def test_unknown_method_returns_result_envelope(raw, state):
    session = signin_raw(raw, state)
    body = raw.get(API + "environment/control/rest/doesnotexist",
                   params={"session": session}).json()
    assert body["result"] != OK
    assert "unknown method" in body["error"]


def test_healthz(raw):
    assert raw.get("/healthz").json()["status"] == "ok"
