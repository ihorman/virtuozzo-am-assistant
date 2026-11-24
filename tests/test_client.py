"""JelasticClient tests: result-code mapping, session passthrough,
fire-and-forget mutations with observable-state polling.

Everything runs against the in-process mock (httpx ASGI transport) — no real
network.
"""

from __future__ import annotations

import time

import httpx
import pytest

from mcp.jelastic_client import (
    AuthFailedError,
    DeleteInProgressError,
    DomainUnavailableError,
    EnvironmentNotFoundError,
    InvalidParameterError,
    JelasticClient,
    ManifestError,
    OperationTimeout,
    RESULT_CODE_MAP,
    SessionExpiredError,
)
from mock.codes import STATUS_CREATING
from tests.helpers import run

JPS_OK = "type: install\nname: hello\n"


async def setup_env(client, name="cenv", **kwargs):
    """Create an environment and wait until it is running."""
    await client.create_env(name, **kwargs)
    return await client.wait_env_status(name, "running", timeout=10)


# --- auth & session passthrough ---------------------------------------------


def test_signin_sets_session(make_client):
    async def scenario():
        async with make_client() as client:
            assert client.session is None
            body = await client.signin("demo", "demo")
            assert body["result"] == 0
            assert client.session and client.session.startswith("mock-sess-")
            assert await client.list_envs() == []
    run(scenario())


def test_signin_bad_password_maps_to_auth_failed(make_client):
    async def scenario():
        async with make_client() as client:
            with pytest.raises(AuthFailedError) as exc:
                await client.signin("demo", "wrong")
            assert exc.value.code == 701
            assert client.session is None
    run(scenario())


def test_session_passthrough_from_constructor(make_client):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            token = client.session
        # second client authenticates purely by token passthrough
        async with make_client(session=token) as client2:
            assert await client2.list_envs() == []
    run(scenario())


def test_expired_session_maps_to_session_expired(make_client, state):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            state.expire_sessions()
            with pytest.raises(SessionExpiredError) as exc:
                await client.list_envs()
            assert exc.value.code == 501
    run(scenario())


def test_from_headers(make_client, app, state):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            token = client.session
        # second client built from the extension contract headers alone
        built = JelasticClient.from_headers(
            {"X-Jelastic-Host": "https://mock.local",
             "X-Jelastic-Session": token},
            transport=httpx.ASGITransport(app=app), poll_interval=0.01)
        async with built:
            assert await built.list_envs() == []
        with pytest.raises(InvalidParameterError):
            JelasticClient.from_headers({})
    run(scenario())


# --- result-code mapping ----------------------------------------------------


def test_env_not_found_maps_to_environment_not_found(make_client):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            with pytest.raises(EnvironmentNotFoundError) as exc:
                await client.env_info("ghost")
            assert exc.value.code == 11  # live: ghost env -> result 11
    run(scenario())


def test_duplicate_env_name_maps_to_domain_unavailable(make_client):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            await setup_env(client, "dup")
            with pytest.raises(DomainUnavailableError) as exc:
                await client.create_env("dup")
            assert exc.value.code == 2314
    run(scenario())


def test_delete_password_rules_map_to_typed_errors(make_client):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            env = await setup_env(client, "pwenv")
            assert env["statusName"] == "running"
            with pytest.raises(InvalidParameterError) as exc:
                await client.delete_env("pwenv", "", wait=False)
            assert "invalid parameter [password]" in exc.value.message
            with pytest.raises(AuthFailedError):
                await client.delete_env("pwenv", "wrong", wait=False)
    run(scenario())


def test_manifest_corruption_maps_to_manifest_error(make_client):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            await setup_env(client, "jpsenv")
            with pytest.raises(ManifestError) as exc:
                await client.install_jps("jpsenv", "type: install\n\tbad: tab\n")
            assert exc.value.code == 11001
    run(scenario())


def test_result_code_map_covers_session_codes():
    assert RESULT_CODE_MAP[501] is SessionExpiredError
    assert RESULT_CODE_MAP[9] is SessionExpiredError
    assert RESULT_CODE_MAP[701] is AuthFailedError
    assert RESULT_CODE_MAP[2212] is DeleteInProgressError


# --- fire-and-forget + observable-state polling -----------------------------


def test_create_is_fire_and_forget_with_polling(make_client, state):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            state.config.transition_seconds = 0.5
            body = await client.create_env("ff")
            assert body["result"] == 0  # acknowledged before completion
            info = await client.env_info("ff")
            # observable async state: live shape is numeric status only
            assert info["status"] == STATUS_CREATING
            assert info["statusName"] != "running"
            info = await client.wait_env_status("ff", "running", timeout=10)
            assert info["statusName"] == "running"
    run(scenario())


def test_wait_env_status_timeout_does_not_conclude_failure(make_client, state):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            state.config.transition_seconds = 1.0
            await client.create_env("slowpoke")
            with pytest.raises(OperationTimeout) as exc:
                await client.wait_env_status("slowpoke", "running", timeout=0.2)
            # the observed state is carried, not a verdict of failure
            assert exc.value.observed["status"] == STATUS_CREATING
            assert "may still be running" in str(exc.value)
            # and the operation indeed still completes afterwards
            info = await client.wait_env_status("slowpoke", "running", timeout=10)
            assert info["statusName"] == "running"
    run(scenario())


def test_delete_env_waits_until_gone_and_tolerates_2212(make_client, state):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            state.config.transition_seconds = 0.5
            await setup_env(client, "goner")
            # first delete starts the async removal
            body = await client.delete_env("goner", "demo", wait=False)
            assert body["result"] == 0
            # a concurrent delete gets 2212 but delete_env treats it as
            # "already on its way out" and still waits for the env to vanish
            body = await client.delete_env("goner", "demo", wait=True, timeout=10)
            assert body["result"] == 0
            with pytest.raises(EnvironmentNotFoundError):
                await client.env_info("goner")
            assert await client.list_envs() == []
    run(scenario())


def test_lifecycle_polling_roundtrip(make_client):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            await setup_env(client, "life")
            await client.stop_env("life")
            info = await client.wait_env_status("life", "stopped", timeout=10)
            assert info["status"] == 0
            await client.start_env("life")
            await client.wait_env_status("life", "running", timeout=10)
            await client.sleep_env("life")
            info = await client.wait_env_status("life", "sleep", timeout=10)
            assert info["status"] == 2
    run(scenario())


# --- topology, deploy, logs, variables --------------------------------------


def test_scale_node_and_change_topology(make_client):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            env = await setup_env(client, "topo")
            node_id = env["nodes"][0]["id"]
            body = await client.scale_node("topo", node_id, 4, 12)
            assert body["result"] == 0
            await client.change_topology("topo", [
                {"nodeType": "cp", "nodeGroup": "cp", "count": 2,
                 "fixedCloudlets": 2, "flexibleCloudlets": 8}])
            env = await client.wait_nodes_ready("topo", timeout=10)
            assert len(env["nodes"]) == 2
            scaled = next(n for n in env["nodes"] if n["id"] == node_id)
            assert (scaled["fixedCloudlets"], scaled["flexibleCloudlets"]) == (2, 8)
    run(scenario())


def test_deploy_jps_logs_and_variables(make_client):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            await setup_env(client, "app")
            body = await client.deploy_archive(
                "app", context="ROOT", url="https://example.com/app.war")
            assert body["result"] == 0
            body = await client.install_jps("app", JPS_OK, settings={"k": "v"})
            assert body["result"] == 0

            # logs: getlogs is a keyword/file listing per node; the content
            # of one file comes from readlog ({linescount, body}).
            groups = await client.get_logs("app")
            assert groups and groups[0]["files"][0]["path"].endswith(".log")
            path = groups[0]["files"][0]["path"]
            content = await client.read_log("app", path, line=5)
            assert "deployed" in content["body"]
            assert content["linescount"] >= 1
            dirs = await client.get_logs_list("app")
            assert dirs and dirs[0]["items"]

            # variables: container env vars by node group (live wire shape)
            await client.set_env_variables("app", {"MODE": "prod"})
            assert await client.get_env_variables("app") == {"MODE": "prod"}
            await client.add_env_variable("app", "TIER", "web")
            await client.remove_env_variable("app", "MODE")
            assert await client.get_env_variables("app") == {"TIER": "web"}
    run(scenario())


def test_env_stats_shape(make_client):
    async def scenario():
        async with make_client() as client:
            await client.signin("demo", "demo")
            await setup_env(client, "stats")
            nodes = await client.env_stats("stats")
            assert nodes and {"nodeid", "cpu", "mem", "disk"} <= set(nodes[0])
            # the live aggregate "sum" row is not a node and is filtered out
            assert all(str(n.get("nodeid")) != "sum" for n in nodes)
    run(scenario())
