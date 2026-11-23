"""Platform authentication for ``virtuozzo-am-mcp login``.

Three flows, all ending in the same 0600 config file (see ``config.py``):

1. **password** — ``users/authentication/rest/signin`` (email + password;
   the password is prompted, never taken on the command line).
2. **session token** — verify an existing dashboard/SSO session and store it.
3. **SSO browser flow** — open the panel for Keycloak/SSO login, then the
   user pastes the session token from the dashboard; verify and store.

All platform calls go through :class:`JelasticClient`, so tests inject an
in-process ``httpx`` transport and never touch the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .config import save_config


class LoginError(Exception):
    """Authentication failed or the platform is unreachable."""


def _client(host: str, session: Optional[str], transport=None):
    from mcp.jelastic_client import JelasticClient

    return JelasticClient(host, session=session, transport=transport)


def _friendly(exc: Exception) -> str:
    from mcp.jelastic_client import AuthFailedError, SessionExpiredError

    if isinstance(exc, AuthFailedError):
        return "authentication failed — check the email/password or session token"
    if isinstance(exc, SessionExpiredError):
        return "session expired — log in to the dashboard again and copy the new token"
    return str(exc)


async def login_with_password(host: str, email: str, password: str, *,
                              transport=None,
                              home: Optional[Path] = None) -> dict:
    """Password flow: sign in, verify, store. Returns the stored config."""
    if not host:
        raise LoginError("--host <panel-url> is required")
    if not email or not password:
        raise LoginError("email and password are required")
    client = _client(host, None, transport)
    try:
        async with client:
            body = await client.signin(email, password)
            session = body.get("session")
            if not session:
                raise LoginError("sign-in returned no session token")
    except LoginError:
        raise
    except Exception as exc:
        raise LoginError(f"sign-in to {host} failed: {_friendly(exc)}") from exc
    path = save_config(host, session, login=email, home=home)
    return {"host": host, "session": session, "login": email,
            "config_path": str(path)}


async def login_with_session(host: str, session: str, *,
                             transport=None,
                             home: Optional[Path] = None) -> dict:
    """Token flow (used for SSO/Keycloak): verify, store."""
    if not host:
        raise LoginError("--host <panel-url> is required")
    if not session:
        raise LoginError("session token is required")
    client = _client(host, session, transport)
    try:
        async with client:
            await client.list_envs()  # verification call: 701 on a bad token
    except Exception as exc:
        raise LoginError(f"session rejected by {host}: {_friendly(exc)}") from exc
    path = save_config(host, session, home=home)
    return {"host": host, "session": session, "config_path": str(path)}


def sso_instructions(host: str) -> list[str]:
    return [
        f"Opening {host} in your browser — sign in with your SSO/Keycloak "
        "account (or the platform login form).",
        "After the dashboard loads, copy your platform session token:",
        "  - from the browser extension's Options page ('Session token'), or",
        "  - from the dashboard: DevTools → Network → any /1.0/ request → "
        "the 'session' query parameter.",
        "Paste the token below (input is hidden from history, not echoed to logs).",
    ]
