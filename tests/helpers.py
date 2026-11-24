"""Shared test helpers (importable as ``tests.helpers``)."""

from __future__ import annotations

import time

from mock.codes import STATUS_CODES

#: mock async transitions complete this fast in tests
TRANSITION = 0.05


def run(coro):
    """Run an async scenario from a sync test (no pytest-asyncio needed)."""
    import asyncio

    return asyncio.run(coro)


def signin_raw(raw, state) -> str:
    """Sign in over REST and return the session token."""
    response = raw.post("/1.0/users/authentication/rest/signin",
                        data={"login": state.config.login,
                              "password": state.config.password})
    body = response.json()
    assert body["result"] == 0, body
    return body["session"]


def wait_status_raw(raw, session, env_name, target, timeout=5.0):
    """Poll getenvinfo until the env reaches the ``target`` status name.

    Live shape: the platform returns a numeric ``status`` and NO statusName,
    with env/nodes/nodeGroups flat at the top level. Returns the env dict
    with ``nodes``/``nodeGroups`` merged in.
    """
    want = STATUS_CODES[target]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = raw.get("/1.0/environment/control/rest/getenvinfo",
                       params={"session": session, "envName": env_name}).json()
        info = body.get("info") or body
        env = dict(info.get("env") or {})
        if env.get("status") == want:
            env["nodes"] = info.get("nodes") or env.get("nodes") or []
            env["nodeGroups"] = (info.get("nodeGroups")
                                 or env.get("nodeGroups") or [])
            return env
        time.sleep(TRANSITION / 2)
    raise AssertionError(f"[{env_name}] never reached status [{target}]")
