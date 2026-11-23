"""Agent detection and MCP config writers.

One entry shape, rendered per agent:

- **Claude Code** — ``~/.claude.json`` → ``mcpServers`` (user scope).
- **Cursor** — ``~/.cursor/mcp.json`` → ``mcpServers``.
- **Hermes** — ``~/.hermes/config.yaml`` → ``mcp_servers`` (YAML block is
  text-spliced in so comments and unrelated keys survive untouched).
- **generic** — a standalone ``{"mcpServers": {...}}`` JSON file.

Two modes:

- **local** — stdio bridge: ``npx -y virtuozzo-am-mcp serve`` with
  ``JELASTIC_HOST`` / ``JELASTIC_SESSION`` env (session also lives in
  ``~/.virtuozzo-am/config.json``, env makes the entry self-contained).
- **remote** — hosted endpoint: ``url`` + ``X-Jelastic-Host`` /
  ``X-Jelastic-Session`` headers (the zero-install variant).
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Optional

SERVER_NAME = "virtuozzo-am"
SERVE_COMMAND = "npx"
SERVE_ARGS = ["-y", "virtuozzo-am-mcp", "serve"]

AGENT_CLAUDE = "claude"
AGENT_CURSOR = "cursor"
AGENT_HERMES = "hermes"
AGENT_GENERIC = "generic"
AGENTS = (AGENT_CLAUDE, AGENT_CURSOR, AGENT_HERMES, AGENT_GENERIC)


class InstallError(Exception):
    """Agent config could not be written."""


# -- detection ---------------------------------------------------------------

def detect_agent(home: Optional[Path] = None) -> str:
    """Best-effort agent detection: claude > cursor > hermes > generic."""
    base = Path(home) if home is not None else Path(os.environ.get("HOME") or Path.home())
    if shutil.which("claude") or (base / ".claude").exists() or (base / ".claude.json").exists():
        return AGENT_CLAUDE
    if shutil.which("cursor") or (base / ".cursor").exists():
        return AGENT_CURSOR
    if shutil.which("hermes") or (base / ".hermes").exists():
        return AGENT_HERMES
    return AGENT_GENERIC


# -- entry builders ----------------------------------------------------------

def local_entry(host: str, session: str) -> dict:
    """stdio bridge entry: command + env auth (self-contained)."""
    return {
        "command": SERVE_COMMAND,
        "args": list(SERVE_ARGS),
        "env": {"JELASTIC_HOST": host, "JELASTIC_SESSION": session},
    }


def remote_entry(mcp_url: str, host: str, session: str) -> dict:
    """Hosted endpoint entry: URL + auth headers."""
    return {
        "url": mcp_url,
        "headers": {
            "X-Jelastic-Host": host,
            "X-Jelastic-Session": session,
        },
    }


def build_entry(mode: str, host: str, session: str,
                mcp_url: Optional[str] = None) -> dict:
    if mode == "remote":
        if not mcp_url:
            raise InstallError("remote mode needs --url <https://assistant-host/mcp>")
        return remote_entry(mcp_url, host, session)
    return local_entry(host, session)


# -- JSON writers (Claude Code / Cursor / generic) ----------------------------

def write_json_servers(path: Path, entry: dict,
                       *, indent: int = 2) -> Path:
    """Merge ``entry`` into ``{"mcpServers": {...}}`` at ``path``.

    Preserves every other key in an existing file. Creates parent dirs.
    """
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InstallError(f"existing {path} is not valid JSON ({exc}); "
                               "fix or move it, then re-run") from exc
        if not isinstance(data, dict):
            raise InstallError(f"existing {path} is not a JSON object")
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise InstallError(f"existing {path} has a non-object 'mcpServers' key")
    servers[SERVER_NAME] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=indent) + "\n", encoding="utf-8")
    return path


def write_claude_config(entry: dict, home: Optional[Path] = None) -> Path:
    base = Path(home) if home is not None else Path(os.environ.get("HOME") or Path.home())
    return write_json_servers(base / ".claude.json", entry)


def write_cursor_config(entry: dict, home: Optional[Path] = None) -> Path:
    base = Path(home) if home is not None else Path(os.environ.get("HOME") or Path.home())
    return write_json_servers(base / ".cursor" / "mcp.json", entry)


def write_generic_config(entry: dict, output: Path) -> Path:
    return write_json_servers(Path(output), entry)


# -- YAML writer (Hermes) ----------------------------------------------------

_MCP_SERVERS_RE = re.compile(r"^mcp_servers:\s*(#.*)?$")
_ENTRY_RE = re.compile(r"^  " + re.escape(SERVER_NAME) + r":\s*(#.*)?$")


def _yaml_scalar(value: str) -> str:
    # JSON string literals are valid YAML flow scalars — one quoting scheme.
    return json.dumps(str(value))


def render_hermes_entry(entry: dict) -> str:
    """Render the ``mcp_servers`` mapping entry as a 2-space-indented block."""
    lines = [f"  {SERVER_NAME}:"]
    for key in ("command", "url"):
        if key in entry:
            lines.append(f"    {key}: {_yaml_scalar(entry[key])}")
    if "args" in entry:
        lines.append("    args: " + json.dumps(list(entry["args"])))
    for map_key in ("env", "headers"):
        if map_key in entry:
            lines.append(f"    {map_key}:")
            for name, value in entry[map_key].items():
                lines.append(f"      {name}: {_yaml_scalar(value)}")
    return "\n".join(lines) + "\n"


def splice_hermes_yaml(text: str, block: str) -> str:
    """Insert/replace the ``virtuozzo-am`` entry inside ``mcp_servers:``.

    Pure text surgery: comments, key order and formatting outside the entry
    are preserved. Assumes top-level ``mcp_servers:`` with entries at 2-space
    indent (the Hermes config format).
    """
    lines = text.splitlines(keepends=True)

    def _strip(line: str) -> str:
        return line.rstrip("\n")

    start = None  # index of the 'virtuozzo-am:' entry line, if present
    anchor = None  # index of the 'mcp_servers:' line, if present
    for idx, line in enumerate(lines):
        stripped = _strip(line)
        if anchor is None and _MCP_SERVERS_RE.match(stripped):
            anchor = idx
        elif anchor is not None and start is None and _ENTRY_RE.match(stripped):
            start = idx
            break

    if anchor is None:
        # No mcp_servers section at all: append one.
        out = text
        if out and not out.endswith("\n"):
            out += "\n"
        if out and not out.endswith("\n\n"):
            out += "\n"
        return out + "mcp_servers:\n" + block

    insert_at = anchor + 1
    if start is not None:
        # Drop the old entry block (its line + deeper-indented/blank lines).
        end = start + 1
        while end < len(lines):
            stripped = _strip(lines[end])
            if stripped.strip() == "":
                end += 1
                continue
            indent = len(stripped) - len(stripped.lstrip(" "))
            if indent <= 2:
                break
            end += 1
        del lines[start:end]
        insert_at = start
    lines.insert(insert_at, block)
    return "".join(lines)


def write_hermes_config(entry: dict, home: Optional[Path] = None) -> Path:
    """Merge the entry into ``~/.hermes/config.yaml`` (text splice, verified)."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml is a hard dep here
        raise InstallError("PyYAML is required to write the Hermes config "
                           "(pip install pyyaml)") from exc

    base = Path(home) if home is not None else Path(os.environ.get("HOME") or Path.home())
    path = base / ".hermes" / "config.yaml"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new = splice_hermes_yaml(old, render_hermes_entry(entry))

    # Verify before touching disk: the result must parse and contain the entry.
    try:
        data = yaml.safe_load(new) or {}
    except yaml.YAMLError as exc:
        raise InstallError(f"refusing to write {path}: merged YAML does not "
                           f"parse ({exc})") from exc
    stored = (data.get("mcp_servers") or {}).get(SERVER_NAME)
    if stored != entry:
        raise InstallError(f"refusing to write {path}: merged YAML does not "
                           "round-trip the server entry")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return path


# -- dispatch + one-liners ---------------------------------------------------

def write_agent_config(agent: str, entry: dict, *,
                       home: Optional[Path] = None,
                       output: Optional[Path] = None) -> Path:
    if agent == AGENT_CLAUDE:
        return write_claude_config(entry, home=home)
    if agent == AGENT_CURSOR:
        return write_cursor_config(entry, home=home)
    if agent == AGENT_HERMES:
        return write_hermes_config(entry, home=home)
    if agent == AGENT_GENERIC:
        if output is None:
            raise InstallError("generic agent needs --output <path>")
        return write_generic_config(entry, Path(output))
    raise InstallError(f"unknown agent {agent!r} (expected one of {', '.join(AGENTS)})")


def one_liners(mode: str, host: str, session: str,
               mcp_url: Optional[str] = None) -> list[str]:
    """Copy-paste manual equivalents, per mode (Claude Code syntax)."""
    if mode == "remote":
        if not mcp_url:
            raise InstallError("remote mode needs --url <https://assistant-host/mcp>")
        return [
            "# zero-install, hosted MCP endpoint (no local bridge needed):",
            f'claude mcp add --transport http {SERVER_NAME} {mcp_url} '
            f'--header "X-Jelastic-Host: {host}" '
            f'--header "X-Jelastic-Session: {session}"',
        ]
    return [
        "# local stdio bridge (reads the session from ~/.virtuozzo-am/config.json):",
        f"claude mcp add {SERVER_NAME} -- npx -y virtuozzo-am-mcp serve --host {host}",
    ]


def hermes_one_liners(mode: str, host: str, session: str,
                      mcp_url: Optional[str] = None) -> list[str]:
    """Hermes manual equivalents.

    ``hermes mcp add`` has no inline ``--header`` flag (remote headers live
    in ``config.yaml``), so the remote variant shows the CLI line plus the
    exact YAML block to paste.
    """
    if mode == "remote":
        if not mcp_url:
            raise InstallError("remote mode needs --url <https://assistant-host/mcp>")
        return [
            f"hermes mcp add {SERVER_NAME} --url {mcp_url} --auth header",
            "# X-Jelastic-* headers cannot be passed on the CLI — they go in "
            "~/.hermes/config.yaml (install already wrote them):",
            "mcp_servers:",
            render_hermes_entry(remote_entry(mcp_url, host, session)).rstrip("\n"),
        ]
    return [
        "# session is read from ~/.virtuozzo-am/config.json at startup:",
        f"hermes mcp add {SERVER_NAME} --command npx --args -y virtuozzo-am-mcp serve",
    ]
