"""Tests for the ``virtuozzo-am-mcp`` onboarding CLI (T9).

Every test runs against a temporary ``HOME`` (config writing) and the
in-process mock platform (login, serve bridge) — no real network, no real
user files.
"""

from __future__ import annotations

import io
import json
import stat

import httpx
import pytest

from tests.helpers import run, signin_raw
from virtuozzo_am_mcp import agents
from virtuozzo_am_mcp.config import (
    ConfigError,
    config_path,
    file_mode,
    load_config,
    save_config,
)
from virtuozzo_am_mcp.login import LoginError, login_with_password, login_with_session
from virtuozzo_am_mcp.main import main, resolve_credentials, run_serve

PANEL = "https://mock.local"
MCP_URL = "https://assistant.example.com/mcp"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """All CLI state lands in a throwaway HOME; no ambient auth leaks in."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("VIRTUOZZO_AM_HOME", raising=False)
    monkeypatch.delenv("JELASTIC_HOST", raising=False)
    monkeypatch.delenv("JELASTIC_SESSION", raising=False)
    return tmp_path


@pytest.fixture
def transport(app):
    return httpx.ASGITransport(app=app)


# -- config file -------------------------------------------------------------

def test_save_config_writes_0600(isolated_home):
    path = save_config(PANEL, "s3cret", login="demo", home=isolated_home)
    assert path == config_path(isolated_home)
    assert file_mode(path) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    data = json.loads(path.read_text())
    assert data["host"] == PANEL
    assert data["session"] == "s3cret"
    assert data["login"] == "demo"
    assert data["version"] == 1


def test_save_config_requires_host_and_session(isolated_home):
    with pytest.raises(ConfigError):
        save_config("", "s", home=isolated_home)
    with pytest.raises(ConfigError):
        save_config(PANEL, "", home=isolated_home)
    assert not config_path(isolated_home).exists()


def test_load_config_roundtrip_and_missing(isolated_home):
    assert load_config(isolated_home) is None
    save_config(PANEL, "tok", home=isolated_home)
    assert load_config(isolated_home)["session"] == "tok"


def test_load_config_rejects_corrupt_file(isolated_home):
    config_path(isolated_home).parent.mkdir(parents=True)
    config_path(isolated_home).write_text("{not json")
    with pytest.raises(ConfigError):
        load_config(isolated_home)


# -- login -------------------------------------------------------------------

def test_login_with_password_stores_config(isolated_home, transport, state):
    result = run(login_with_password(PANEL, state.config.login,
                                     state.config.password,
                                     transport=transport, home=isolated_home))
    assert result["host"] == PANEL
    assert result["login"] == state.config.login
    cfg = load_config(isolated_home)
    assert cfg["session"] == result["session"]
    assert file_mode(config_path(isolated_home)) == 0o600


def test_login_with_password_rejects_bad_password(isolated_home, transport):
    with pytest.raises(LoginError, match="authentication failed"):
        run(login_with_password(PANEL, "demo", "wrong",
                                transport=transport, home=isolated_home))
    assert not config_path(isolated_home).exists()


def test_login_with_session_verifies_token(isolated_home, transport, raw, state):
    token = signin_raw(raw, state)
    result = run(login_with_session(PANEL, token,
                                    transport=transport, home=isolated_home))
    assert result["session"] == token
    assert load_config(isolated_home)["session"] == token

    with pytest.raises(LoginError, match="session rejected"):
        run(login_with_session(PANEL, "no-such-token",
                               transport=transport, home=isolated_home))


def test_cli_login_session_flag(isolated_home, transport, raw, state):
    token = signin_raw(raw, state)
    rc = main(["login", "--host", PANEL, "--session", token],
              transport=transport, home=isolated_home)
    assert rc == 0
    assert load_config(isolated_home)["session"] == token


# -- install: writers --------------------------------------------------------

def test_install_claude_local(isolated_home, capsys):
    save_config(PANEL, "tok", home=isolated_home)
    rc = main(["install", "--agent", "claude"], home=isolated_home)
    assert rc == 0
    cfg = json.loads((isolated_home / ".claude.json").read_text())
    entry = cfg["mcpServers"]["virtuozzo-am"]
    assert entry["command"] == "npx"
    assert entry["args"] == ["-y", "virtuozzo-am-mcp", "serve"]
    assert entry["env"] == {"JELASTIC_HOST": PANEL, "JELASTIC_SESSION": "tok"}
    out = capsys.readouterr().out
    assert "claude mcp add virtuozzo-am -- npx -y virtuozzo-am-mcp serve" in out


def test_install_claude_remote(isolated_home, capsys):
    save_config(PANEL, "tok", home=isolated_home)
    rc = main(["install", "--agent", "claude", "--mode", "remote",
               "--url", MCP_URL], home=isolated_home)
    assert rc == 0
    entry = json.loads((isolated_home / ".claude.json").read_text())[
        "mcpServers"]["virtuozzo-am"]
    assert entry["url"] == MCP_URL
    assert entry["headers"] == {
        "X-Jelastic-Host": PANEL, "X-Jelastic-Session": "tok"}
    out = capsys.readouterr().out
    assert "claude mcp add --transport http virtuozzo-am " + MCP_URL in out
    assert '--header "X-Jelastic-Session: tok"' in out


def test_install_claude_preserves_existing_keys(isolated_home):
    save_config(PANEL, "tok", home=isolated_home)
    (isolated_home / ".claude.json").write_text(
        json.dumps({"theme": "dark", "mcpServers": {"other": {"command": "x"}}}))
    assert main(["install", "--agent", "claude"], home=isolated_home) == 0
    cfg = json.loads((isolated_home / ".claude.json").read_text())
    assert cfg["theme"] == "dark"
    assert "other" in cfg["mcpServers"]
    assert "virtuozzo-am" in cfg["mcpServers"]


def test_install_cursor_local(isolated_home):
    save_config(PANEL, "tok", home=isolated_home)
    rc = main(["install", "--agent", "cursor"], home=isolated_home)
    assert rc == 0
    cfg = json.loads((isolated_home / ".cursor" / "mcp.json").read_text())
    entry = cfg["mcpServers"]["virtuozzo-am"]
    assert entry["command"] == "npx"
    assert entry["env"]["JELASTIC_SESSION"] == "tok"


def test_install_generic_json(isolated_home, capsys):
    save_config(PANEL, "tok", home=isolated_home)
    out_file = isolated_home / "mcp-config.json"
    rc = main(["install", "--agent", "generic",
               "--output", str(out_file)], home=isolated_home)
    assert rc == 0
    cfg = json.loads(out_file.read_text())
    assert cfg["mcpServers"]["virtuozzo-am"]["command"] == "npx"
    out = capsys.readouterr().out
    assert str(out_file) in out


def test_install_generic_remote_header_shape(isolated_home):
    save_config(PANEL, "tok", home=isolated_home)
    out_file = isolated_home / "remote.json"
    rc = main(["install", "--agent", "generic", "--mode", "remote",
               "--url", MCP_URL, "--output", str(out_file)],
              home=isolated_home)
    assert rc == 0
    entry = json.loads(out_file.read_text())["mcpServers"]["virtuozzo-am"]
    assert entry == {"url": MCP_URL, "headers": {
        "X-Jelastic-Host": PANEL, "X-Jelastic-Session": "tok"}}


# -- install: Hermes YAML splice ---------------------------------------------

HERMES_YAML = """\
# my hermes config
model:
  provider: openrouter
  default: some/model
mcp_servers:
  existing-tool:
    command: node
    args: ["server.js"]
    env:
      KEY: value
display:
  skin: dark
"""


def _hermes_yaml(home):
    import yaml

    path = home / ".hermes" / "config.yaml"
    return path, yaml.safe_load(path.read_text())


def test_install_hermes_writes_valid_entry(isolated_home, capsys):
    save_config(PANEL, "tok", home=isolated_home)
    cfg_path = isolated_home / ".hermes" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(HERMES_YAML)

    rc = main(["install", "--agent", "hermes"], home=isolated_home)
    assert rc == 0

    path, data = _hermes_yaml(isolated_home)
    entry = data["mcp_servers"]["virtuozzo-am"]
    assert entry == {
        "command": "npx",
        "args": ["-y", "virtuozzo-am-mcp", "serve"],
        "env": {"JELASTIC_HOST": PANEL, "JELASTIC_SESSION": "tok"},
    }
    # unrelated keys and the existing server survive the splice
    assert data["model"]["provider"] == "openrouter"
    assert data["mcp_servers"]["existing-tool"]["command"] == "node"
    assert data["display"]["skin"] == "dark"
    text = path.read_text()
    assert "# my hermes config" in text
    out = capsys.readouterr().out
    assert "hermes mcp add virtuozzo-am --command npx" in out


def test_install_hermes_replaces_existing_entry(isolated_home):
    save_config(PANEL, "old", home=isolated_home)
    cfg_path = isolated_home / ".hermes" / "config.yaml"
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text(HERMES_YAML)

    assert main(["install", "--agent", "hermes"], home=isolated_home) == 0
    save_config(PANEL, "new", home=isolated_home)
    assert main(["install", "--agent", "hermes"], home=isolated_home) == 0

    text = cfg_path.read_text()
    assert text.count("  virtuozzo-am:") == 1
    _, data = _hermes_yaml(isolated_home)
    assert data["mcp_servers"]["virtuozzo-am"]["env"]["JELASTIC_SESSION"] == "new"
    assert "existing-tool" in data["mcp_servers"]


def test_install_hermes_creates_config(isolated_home):
    save_config(PANEL, "tok", home=isolated_home)
    assert main(["install", "--agent", "hermes"], home=isolated_home) == 0
    _, data = _hermes_yaml(isolated_home)
    assert data["mcp_servers"]["virtuozzo-am"]["command"] == "npx"


def test_install_hermes_remote(isolated_home, capsys):
    save_config(PANEL, "tok", home=isolated_home)
    rc = main(["install", "--agent", "hermes", "--mode", "remote",
               "--url", MCP_URL], home=isolated_home)
    assert rc == 0
    _, data = _hermes_yaml(isolated_home)
    entry = data["mcp_servers"]["virtuozzo-am"]
    assert entry["url"] == MCP_URL
    assert entry["headers"]["X-Jelastic-Session"] == "tok"
    out = capsys.readouterr().out
    assert "hermes mcp add virtuozzo-am --url " + MCP_URL in out


# -- install: guard rails + detection ----------------------------------------

def test_install_requires_login(isolated_home, capsys):
    rc = main(["install", "--agent", "generic",
               "--output", str(isolated_home / "x.json")], home=isolated_home)
    assert rc == 1
    assert "login" in capsys.readouterr().err


def test_install_remote_requires_url(isolated_home, capsys):
    save_config(PANEL, "tok", home=isolated_home)
    rc = main(["install", "--agent", "generic", "--mode", "remote",
               "--output", str(isolated_home / "x.json")], home=isolated_home)
    assert rc == 1
    assert "--url" in capsys.readouterr().err


@pytest.mark.parametrize("marker,expected", [
    (".claude", "claude"),
    (".cursor", "cursor"),
    (".hermes", "hermes"),
])
def test_detect_agent_by_marker(isolated_home, monkeypatch, marker, expected):
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    (isolated_home / marker).mkdir()
    assert agents.detect_agent(home=isolated_home) == expected


def test_detect_agent_falls_back_to_generic(isolated_home, monkeypatch):
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    assert agents.detect_agent(home=isolated_home) == "generic"


def test_install_auto_detects_agent(isolated_home, monkeypatch):
    monkeypatch.setattr(agents.shutil, "which", lambda name: None)
    save_config(PANEL, "tok", home=isolated_home)
    (isolated_home / ".cursor").mkdir()
    assert main(["install"], home=isolated_home) == 0
    cfg = json.loads((isolated_home / ".cursor" / "mcp.json").read_text())
    assert "virtuozzo-am" in cfg["mcpServers"]


# -- credential resolution ---------------------------------------------------

def test_resolve_credentials_flag_over_config(isolated_home, monkeypatch):
    save_config(PANEL, "cfg-tok", home=isolated_home)
    monkeypatch.setenv("JELASTIC_HOST", "https://env-host")
    monkeypatch.setenv("JELASTIC_SESSION", "env-tok")
    assert resolve_credentials(home=isolated_home) == (
        "https://env-host", "env-tok")
    assert resolve_credentials("https://flag-host", "flag-tok",
                               home=isolated_home) == (
        "https://flag-host", "flag-tok")


def test_resolve_credentials_missing(isolated_home):
    with pytest.raises(LoginError, match="login"):
        resolve_credentials(home=isolated_home)


# -- serve: stdio bridge -----------------------------------------------------

def _stdio_request(req_id, method, params=None):
    return json.dumps({"jsonrpc": "2.0", "id": req_id,
                       "method": method, "params": params or {}})


def _responses(stdout: io.StringIO) -> dict:
    out = {}
    for line in stdout.getvalue().splitlines():
        msg = json.loads(line)
        if "id" in msg:
            out[msg["id"]] = msg
    return out


def test_serve_bridge_full_handshake(isolated_home, transport, raw, state):
    token = signin_raw(raw, state)
    save_config(PANEL, token, home=isolated_home)

    stdin = io.StringIO("\n".join([
        _stdio_request(1, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        _stdio_request(2, "tools/list"),
        _stdio_request(3, "tools/call",
                       {"name": "envs_list", "arguments": {}}),
    ]) + "\n")
    stdout = io.StringIO()
    run_serve(PANEL, token, stdin=stdin, stdout=stdout, transport=transport)

    from mcp.tools import TOOLS

    msgs = _responses(stdout)
    assert msgs[1]["result"]["serverInfo"]["name"] == "virtuozzo-am-mcp"
    listed = {t["name"] for t in msgs[2]["result"]["tools"]}
    assert listed == set(TOOLS)  # same tool surface as mcp/ server
    assert len(listed) == len(TOOLS)
    call = msgs[3]["result"]
    assert call.get("isError") is not True, call
    # auth passthrough: the call reached the platform as the logged-in user
    assert "no_credentials" not in json.dumps(call)


def test_serve_reads_config_without_flags(isolated_home, transport, raw, state):
    """`serve` with no flags resolves host+session from ~/.virtuozzo-am."""
    token = signin_raw(raw, state)
    save_config(PANEL, token, home=isolated_home)

    stdin = io.StringIO(_stdio_request(1, "tools/list") + "\n")
    stdout = io.StringIO()
    rc = main(["serve"], transport=transport, home=isolated_home,
              stdin=stdin, stdout=stdout)
    assert rc == 0
    msgs = _responses(stdout)
    from mcp.tools import TOOLS
    assert len(msgs[1]["result"]["tools"]) == len(TOOLS)


def test_serve_without_credentials_fails_clearly(isolated_home, capsys):
    rc = main(["serve"], home=isolated_home)
    assert rc == 1
    assert "login" in capsys.readouterr().err
