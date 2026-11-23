"""Stored platform session: ``~/.virtuozzo-am/config.json`` (0600).

The config file is the single place the CLI keeps the platform session
token. ``serve`` reads it at runtime, so an agent MCP entry only needs the
panel URL; the session itself is passed via ``JELASTIC_SESSION`` env (or
looked up here when absent).

``VIRTUOZZO_AM_HOME`` overrides the config directory (used by tests).
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

CONFIG_DIR_NAME = ".virtuozzo-am"
CONFIG_FILE_NAME = "config.json"
CONFIG_VERSION = 1

FILE_MODE = 0o600
DIR_MODE = 0o700


class ConfigError(Exception):
    """Unreadable or invalid config file."""


def config_dir(home: Optional[Path] = None) -> Path:
    """Directory holding ``config.json`` (0700)."""
    override = os.environ.get("VIRTUOZZO_AM_HOME")
    if override:
        return Path(override)
    if home is not None:
        base = Path(home)
    else:
        base = Path(os.environ.get("HOME") or Path.home())
    return base / CONFIG_DIR_NAME


def config_path(home: Optional[Path] = None) -> Path:
    return config_dir(home) / CONFIG_FILE_NAME


def save_config(host: str, session: str,
                login: Optional[str] = None,
                *, home: Optional[Path] = None) -> Path:
    """Persist the platform session with 0600 permissions.

    Returns the config file path.
    """
    if not host:
        raise ConfigError("host is required")
    if not session:
        raise ConfigError("session is required")
    directory = config_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, DIR_MODE)
    path = directory / CONFIG_FILE_NAME
    payload: dict[str, Any] = {
        "version": CONFIG_VERSION,
        "host": host,
        "session": session,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if login:
        payload["login"] = login
    # Write via a temp file so a crash can never leave a half-written config.
    tmp = directory / (CONFIG_FILE_NAME + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, FILE_MODE)
    os.replace(tmp, path)
    os.chmod(path, FILE_MODE)
    return path


def load_config(home: Optional[Path] = None) -> Optional[dict]:
    """Read the stored config, or ``None`` when absent.

    Raises :class:`ConfigError` when the file exists but is not valid JSON
    with at least ``host`` and ``session``.
    """
    path = config_path(home)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or not data.get("host") or not data.get("session"):
        raise ConfigError(f"{path} is missing 'host'/'session' — re-run "
                          "`virtuozzo-am-mcp login --host <panel-url>`")
    return data


def file_mode(path: Path) -> int:
    """Permission bits of ``path`` (handy for tests and diagnostics)."""
    return stat.S_IMODE(path.stat().st_mode)
