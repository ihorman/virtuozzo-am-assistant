"""Mock Jelastic platform package.

Public surface:
- :class:`MockJelastic` / :class:`MockConfig` — in-memory state machine
- :func:`create_app` — FastAPI app exposing the REST subset
- :mod:`mock.codes` — result/status code constants
"""

from .codes import (  # noqa: F401
    AUTH_FAILED,
    DELETE_IN_PROGRESS,
    DOMAIN_UNAVAILABLE,
    ENV_NOT_FOUND,
    MANIFEST_CORRUPTED,
    OK,
    SESSION_CODES,
    SESSION_INVALID,
)
from .server import create_app
from .state import Env, MockConfig, MockJelastic, Node

__all__ = [
    "MockJelastic",
    "MockConfig",
    "Node",
    "Env",
    "create_app",
    "OK",
    "SESSION_INVALID",
    "AUTH_FAILED",
    "DELETE_IN_PROGRESS",
    "DOMAIN_UNAVAILABLE",
    "ENV_NOT_FOUND",
    "MANIFEST_CORRUPTED",
    "SESSION_CODES",
]
