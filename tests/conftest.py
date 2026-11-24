"""Shared pytest fixtures for the Virtuozzo Application Management Assistant suite.

All tests run against the in-process mock (ASGI transport) — no real network.
The repo root is put on ``sys.path`` first so ``mock`` and ``mcp`` resolve to
this repo's packages, never to same-named PyPI packages.
"""

from __future__ import annotations

import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402
import pytest  # noqa: E402

from mock import MockConfig, MockJelastic, create_app  # noqa: E402
from mcp.jelastic_client import JelasticClient  # noqa: E402
from tests.helpers import TRANSITION  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    """Hard guarantee: tests never open real network connections.

    Local/Unix sockets stay allowed (asyncio internals use socketpairs); any
    TCP connect to a non-loopback address fails the test.
    """
    import socket

    real_connect = socket.socket.connect

    def guarded(sock, address):
        if isinstance(address, tuple) and address[0] not in (
                "127.0.0.1", "::1", "localhost"):
            raise AssertionError(
                f"test attempted a real network connect to {address}")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """No real DNS in tests: the SSRF guard's resolver seam is stubbed to a
    stable public address. Security tests override the seam per-case to
    simulate private/metadata resolutions."""
    import mcp.security as security

    monkeypatch.setattr(security, "resolve_host_ips",
                        lambda host: ["93.184.216.34"])


@pytest.fixture
def state() -> MockJelastic:
    return MockJelastic(MockConfig(transition_seconds=TRANSITION))


@pytest.fixture
def app(state):
    return create_app(state)


@pytest.fixture
def raw(app):
    """Sync HTTP client hitting the mock like a real REST client would."""
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture
def make_client(app):
    """Factory of async :class:`JelasticClient` instances bound to the mock."""

    def _make(session: str | None = None, **kwargs) -> JelasticClient:
        kwargs.setdefault("poll_interval", 0.01)
        kwargs.setdefault("transport", httpx.ASGITransport(app=app))
        return JelasticClient("https://mock.local", session=session, **kwargs)

    return _make
