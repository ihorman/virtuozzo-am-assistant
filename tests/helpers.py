"""Shared test helpers (importable as ``tests.helpers``)."""

from __future__ import annotations

import time

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
    """Poll getenvinfo over REST until the env reaches ``target`` status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = raw.get("/1.0/environment/control/rest/getenvinfo",
                       params={"session": session, "envName": env_name}).json()
        env = body.get("env") or {}
        if env.get("statusName") == target:
            return env
        time.sleep(TRANSITION / 2)
    raise AssertionError(f"[{env_name}] never reached status [{target}]")
