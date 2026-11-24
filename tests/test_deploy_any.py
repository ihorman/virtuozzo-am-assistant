"""T12 acceptance: deploy ANY application - detection, tiered manifests,
VCS deploy, healthcheck, and the deploy-anything wizard end-to-end.

Everything runs against the in-process mock (ASGI transport, no network -
enforced by the conftest socket guard; the source-fetch seam is stubbed
per-case). Covered per the T12 acceptance:

- detection table: every manifest/toolchain file -> type/version,
- SSRF + caps on project_inspect,
- confirm gating on deploy_vcs / deploy_any,
- generated JPS YAML is parseable + ASCII-only + round-trips base64 scripts,
- app_healthcheck error classes,
- FakeLLM flow test 'deploy my Rust app from <git url>' end-to-end
  (detection chip -> runtime image -> healthcheck verify).
"""

from __future__ import annotations

import base64
import re

import httpx
import pytest
import yaml

import mcp.tools as mcp_tools
from mcp import deploy_templates
from mcp.jelastic_client import JelasticError
from mcp.tools import AuthConfig, execute_tool
from mock import MockJelastic
from mock.fixtures import source_zip
from tests.helpers import run, signin_raw
from tests.test_flows import (
    FINISH,
    FakeLLM,
    FakeVerifier,
    assert_contract,
    cards,
    drive_chat,
    llm_flow,
    make_agent,
    make_env,
    scenario_headers,
    texts,
    tool_calls,
)

# --- helpers ----------------------------------------------------------------


def _call(app, auth, name, arguments):
    return run(execute_tool(name, arguments, auth,
                            transport=httpx.ASGITransport(app=app),
                            timeout=5.0, poll_interval=0.01))


def _auth(raw, state) -> AuthConfig:
    return AuthConfig(host="https://mock.local",
                      session=signin_raw(raw, state))


def _zip_fetcher(language: str, seen: list):
    """Fetch seam serving a fixture source zip (for source URLs) and a
    healthy page (for app URLs, i.e. app_healthcheck)."""
    async def fake_fetch(url, *, timeout=10.0, max_bytes=1 << 25):
        seen.append(url)
        if "github.com" in url or url.endswith((".zip", ".tar.gz", ".tgz")):
            return {"status": 200, "headers": {},
                    "body": source_zip(language), "url": url,
                    "truncated": False}
        return {"status": 200, "headers": {"content-type": "text/html"},
                "body": (b"<html><head><title>Mock app</title></head>"
                         b"<body>ok</body></html>"),
                "url": url, "truncated": False}

    return fake_fetch


# --- detection table --------------------------------------------------------


DETECTION_TABLE = [
    ({"package.json": '{"engines": {"node": ">=20"}}'}, "node", "20"),
    ({"requirements.txt": "flask==3.0.0\n", ".python-version": "3.12\n"},
     "python", "3.12"),
    ({"pom.xml": "<properties><java.version>21</java.version></properties>"},
     "java", "21"),
    ({"composer.json": '{"require": {"php": ">=8.2"}}'}, "php", "8.2"),
    ({"Gemfile": 'ruby "3.2.2"\n'}, "ruby", "3.2"),
    ({"go.mod": "module x\n\ngo 1.23\n"}, "go", "1.23"),
    ({"app.csproj": "<TargetFramework>net8.0</TargetFramework>"},
     "dotnet", "8.0"),
    ({"Cargo.toml": '[package]\nrust-version = "1.80"\n'}, "rust", "1.80"),
    ({"mix.exs": 'def project, do: [elixir: "~> 1.17"]\n'}, "elixir", "1.17"),
    ({"build.zig": "const std = @import(\"std\");\n"}, "zig", None),
    ({"CMakeLists.txt": "project(x)\n"}, "c_cpp", None),
    ({"Makefile": "all:\n"}, "c_cpp", None),
    ({"deno.json": "{}\n"}, "deno", None),
    ({"bunfig.toml": "[install]\n"}, "bun", None),
    ({"Dockerfile": "FROM node:20\n"}, "docker", None),
    ({"index.html": "<html></html>"}, "static", None),
    ({"stack.yaml": "resolver: lts-21\n"}, "haskell", None),
    ({"dune-project": "(lang dune 3.0)\n"}, "ocaml", None),
    ({"shard.yml": "name: x\ncrystal: 1.10.0\n"}, "crystal", "1.10"),
    ({"pubspec.yaml": "environment:\n  sdk: '>=3.0.0 <4.0.0'\n"},
     "dart", "3.0"),
    ({"Package.swift": "// swift-tools-version:5.9\n"}, "swift", "5.9"),
    ({"app.nimble": "version = \"0.1.0\"\n"}, "nim", None),
]


@pytest.mark.parametrize("files,type_,version", DETECTION_TABLE)
def test_detection_table(files, type_, version):
    result = deploy_templates.detect_project(files)
    assert result["detected"], f"no detection for {files}"
    first = result["detected"][0]
    assert first["type"] == type_
    assert first["version"] == version
    assert first["evidence"]  # evidence file is always named
    if version:
        assert result["confidence"] == "high"
        assert version in (result["suggested_stack"] or "")
    assert result["suggested_runtime_image"] is not None or type_ == "docker"


def test_detection_priority_language_over_fallbacks():
    files = {"package.json": "{}", "Makefile": "all:\n",
             "Dockerfile": "FROM node:20\n", "index.html": "<html>"}
    result = deploy_templates.detect_project(files)
    types = [d["type"] for d in result["detected"]]
    assert types[0] == "node"  # language wins over fallback markers


def test_markerless_source_has_no_suggestion():
    result = deploy_templates.detect_project({"main.weirdlang": "x"})
    assert result["detected"] == []
    assert result["suggested_stack"] is None
    assert result["suggested_runtime_image"] is None
    assert result["confidence"] == "low"


# --- project_inspect --------------------------------------------------------


def test_project_inspect_detects_rust_zip(app, raw, state, monkeypatch):
    seen = []
    monkeypatch.setattr(mcp_tools, "http_fetch",
                        _zip_fetcher("rust", seen))
    auth = _auth(raw, state)
    payload, is_error = _call(app, auth, "project_inspect",
                              {"source_url": "https://example.com/app.tar.gz"})
    assert is_error is False, payload
    assert payload["source_kind"] == "archive"
    assert payload["detected"][0] == {"type": "rust", "version": "1.80",
                                      "evidence": "Cargo.toml"}
    assert payload["suggested_stack"] == "rust:1.80"
    assert payload["suggested_runtime_image"] == "rust:1.80-slim"
    assert payload["suggested_port"] == 8080  # axum convention
    assert payload["confidence"] == "high"


def test_project_inspect_git_url_maps_to_archive(app, raw, state,
                                                 monkeypatch):
    seen = []
    monkeypatch.setattr(mcp_tools, "http_fetch",
                        _zip_fetcher("node", seen))
    auth = _auth(raw, state)
    payload, is_error = _call(
        app, auth, "project_inspect",
        {"source_url": "git@github.com:acme/node-demo.git"})
    assert is_error is False, payload
    assert payload["source_kind"] == "git"
    assert payload["source_url"] == "https://github.com/acme/node-demo"
    # the git URL was mapped to the host's default-branch archive endpoint
    assert seen[0] == ("https://github.com/acme/node-demo"
                       "/archive/HEAD.tar.gz")
    assert payload["detected"][0]["type"] == "node"
    assert payload["detected"][0]["version"] == "20"


def test_project_inspect_default_branch_from_redirect(app, raw, state,
                                                      monkeypatch):
    async def fake_fetch(url, *, timeout=10.0, max_bytes=1 << 25):
        if url.endswith("HEAD.tar.gz"):
            return {"status": 302,
                    "headers": {"location":
                                "https://codeload.github.com/acme/rust-demo"
                                "/tar.gz/refs/heads/trunk"},
                    "body": b"", "url": url, "truncated": False}
        return {"status": 200, "headers": {},
                "body": source_zip("rust"), "url": url, "truncated": False}

    monkeypatch.setattr(mcp_tools, "http_fetch", fake_fetch)
    auth = _auth(raw, state)
    payload, is_error = _call(
        app, auth, "project_inspect",
        {"source_url": "https://github.com/acme/rust-demo"})
    assert is_error is False, payload
    assert payload["default_branch"] == "trunk"


def test_project_inspect_ssrf_and_scheme_refused(app, raw, state):
    auth = _auth(raw, state)
    for url in ("http://127.0.0.1/x.zip", "http://169.254.169.254/latest",
                "ftp://example.com/x.zip", "file:///etc/passwd"):
        payload, is_error = _call(app, auth, "project_inspect",
                                  {"source_url": url})
        assert is_error is True, (url, payload)
        assert payload["error"]["code"] in ("url_blocked", "invalid_parameter")


def test_project_inspect_private_resolution_refused(app, raw, state,
                                                    monkeypatch):
    import mcp.security as security

    monkeypatch.setattr(security, "resolve_host_ips",
                        lambda host: ["10.0.0.5"])
    auth = _auth(raw, state)
    payload, is_error = _call(app, auth, "project_inspect",
                              {"source_url": "https://example.com/x.zip"})
    assert is_error is True
    assert payload["error"]["code"] == "url_blocked"


def test_project_inspect_size_caps(app, raw, state, monkeypatch):
    auth = _auth(raw, state)

    # 1. the declared/downloaded size cap (25 MB)
    async def huge_fetch(url, *, timeout=10.0, max_bytes=1 << 25):
        return {"status": 200, "headers": {"content-length": "999999999"},
                "body": b"", "url": url, "truncated": True}

    monkeypatch.setattr(mcp_tools, "http_fetch", huge_fetch)
    payload, is_error = _call(app, auth, "project_inspect",
                              {"source_url": "https://example.com/big.zip"})
    assert is_error is True
    assert ("cap" in payload["error"]["message"]
            or "large" in payload["error"]["message"])

    # 2. the entry-count cap (zip bomb)
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index in range(deploy_templates.MAX_ENTRIES + 10):
            archive.writestr(f"f{index}.txt", "x")
    bomb = buffer.getvalue()

    async def bomb_fetch(url, *, timeout=10.0, max_bytes=1 << 25):
        return {"status": 200, "headers": {}, "body": bomb, "url": url,
                "truncated": False}

    monkeypatch.setattr(mcp_tools, "http_fetch", bomb_fetch)
    with pytest.raises(deploy_templates.SourceTooLargeError):
        deploy_templates.read_archive_entries(bomb, "bomb.zip")


# --- confirm gating ---------------------------------------------------------


def test_deploy_vcs_confirm_gating(app, raw, state):
    auth = _auth(raw, state)
    make_env(state, "gate-vcs")
    before = list(state.calls)
    payload, is_error = _call(app, auth, "deploy_vcs", {
        "env_name": "gate-vcs", "git_url": "https://github.com/acme/demo"})
    assert is_error is True
    assert payload["error"]["code"] == "confirm_required"
    assert state.calls == before  # nothing reached the platform


def test_deploy_any_confirm_gating(app, raw, state):
    auth = _auth(raw, state)
    make_env(state, "gate-any")
    before = list(state.calls)
    payload, is_error = _call(app, auth, "deploy_any", {
        "env_name": "gate-any", "source_url": "https://github.com/acme/demo",
        "runtime_image": "rust:1.80-slim"})
    assert is_error is True
    assert payload["error"]["code"] == "confirm_required"
    assert state.calls == before


# --- deploy_vcs / deploy_any against the mock -------------------------------


def test_deploy_vcs_registers_deploys_syncs(app, raw, state):
    auth = _auth(raw, state)
    make_env(state, "vcs-app")
    state.calls.clear()
    payload, is_error = _call(app, auth, "deploy_vcs", {
        "env_name": "vcs-app", "git_url": "https://github.com/acme/demo",
        "branch": "main", "context": "ROOT", "confirm": True})
    assert is_error is False, payload
    assert payload["steps"] == ["addrepo", "deploy", "update"]
    for method in ("environment/deployment/rest/addrepo",
                   "environment/deployment/rest/deploy",
                   "environment/deployment/rest/update"):
        assert method in state.calls
    env = state.envs["vcs-app"]
    assert env.vcs["url"] == "https://github.com/acme/demo"
    assert env.vcs["branch"] == "main"


def test_deploy_any_installs_generated_manifest(app, raw, state):
    auth = _auth(raw, state)
    make_env(state, "any-app")
    state.calls.clear()
    payload, is_error = _call(app, auth, "deploy_any", {
        "env_name": "any-app",
        "source_url": "https://github.com/acme/rust-demo.git",
        "runtime_image": "rust:1.80-slim", "port": 8080, "confirm": True})
    assert is_error is False, payload
    assert payload["tier"] == 2
    assert payload["runtime_image"] == "rust:1.80-slim"
    assert "marketplace/jps/rest/install" in state.calls
    env = state.envs["any-app"]
    assert len(env.installations) == 1
    manifest = yaml.safe_load(payload["manifest"])
    assert manifest["nodes"]["cp"]["image"] == "rust:1.80-slim"
    assert manifest["env"]["PORT"] == "8080"


def test_deploy_any_unknown_source_requires_image(app, raw, state,
                                                  monkeypatch):
    monkeypatch.setattr(mcp_tools, "http_fetch",
                        _zip_fetcher("unknown", []))
    auth = _auth(raw, state)
    make_env(state, "exotic-app")
    payload, is_error = _call(app, auth, "deploy_any", {
        "env_name": "exotic-app", "source_url": "https://example.com/x.zip",
        "confirm": True})
    assert is_error is True
    assert "runtime_image" in payload["error"]["message"]

    # ...and the wizard's answers always yield a deployable plan:
    payload, is_error = _call(app, auth, "deploy_any", {
        "env_name": "exotic-app", "source_url": "https://example.com/x.zip",
        "runtime_image": "vps", "confirm": True})
    assert is_error is False, payload
    assert payload["tier"] == 4
    manifest = yaml.safe_load(payload["manifest"])
    assert manifest["nodes"]["cp"]["nodeType"] == "vps"

    payload, is_error = _call(app, auth, "deploy_any", {
        "env_name": "exotic-app", "source_url": "https://example.com/x.zip",
        "runtime_image": "myregistry.example.com/exotic:1", "confirm": True})
    assert is_error is False, payload
    assert payload["tier"] == 3
    assert payload["runtime_image"] == "myregistry.example.com/exotic:1"


# --- generated JPS YAML: parseable, ASCII-only, base64 round-trip -----------


def _extract_b64(manifest_text: str) -> list:
    return re.findall(r"echo ([A-Za-z0-9+/=]+) \| base64 -d", manifest_text)


def test_runtime_manifest_yaml_ascii_roundtrip():
    manifest_text = deploy_templates.build_runtime_manifest(
        name="demo", image="rust:1.80-slim", port=8080,
        source={"git_url": "https://github.com/acme/rust-demo.git",
                "branch": "main"},
        language="rust")
    assert yaml.safe_load(manifest_text)  # parseable
    assert all(ord(c) < 127 for c in manifest_text)  # ASCII-only
    assert "\t" not in manifest_text

    manifest = yaml.safe_load(manifest_text)
    actions = manifest["actions"]["onInstall"]
    assert len(actions) == 3  # fetch, build, start
    scripts = deploy_templates.embedded_scripts(manifest_text)
    assert len(scripts) == 3
    # fetch script clones the repo on the given branch
    assert "git clone" in scripts[0] and "'main'" in scripts[0]
    # build script builds inside the container (no image build)
    assert "cargo build --release" in scripts[1]
    # start script binds 0.0.0.0 and runs detached
    assert "0.0.0.0" in scripts[2] and "PORT" in scripts[2]
    assert "nohup setsid" in actions[2]["cmd [cp]"]
    # round-trip: re-encoding the decoded scripts reproduces the manifest
    assert _extract_b64(manifest_text) == [
        base64.b64encode(script.encode("utf-8")).decode("ascii")
        for script in scripts]


def test_vps_manifest_yaml_ascii_roundtrip():
    manifest_text = deploy_templates.build_vps_manifest(
        name="demo", port=8080,
        source={"archive_url": "https://example.com/app.tar.gz"},
        language="zig")
    manifest = yaml.safe_load(manifest_text)
    assert manifest["nodes"]["cp"]["nodeType"] == "vps"
    assert all(ord(c) < 127 for c in manifest_text) and "\t" not in \
        manifest_text
    scripts = deploy_templates.embedded_scripts(manifest_text)
    assert len(scripts) == 1
    assert "apt-get install" in scripts[0]  # toolchain bootstrap
    assert "curl -fsSL" in scripts[0] and "zig build" in scripts[0]
    assert "nohup setsid" in scripts[0]
    assert _extract_b64(manifest_text) == [
        base64.b64encode(scripts[0].encode("utf-8")).decode("ascii")]


# --- app_healthcheck error classes ------------------------------------------


def _healthcheck(app, raw, state, monkeypatch, fetch, env_name="hc-app"):
    monkeypatch.setattr(mcp_tools, "http_fetch", fetch)
    auth = _auth(raw, state)
    return _call(app, auth, "app_healthcheck", {"env_name": env_name})


def test_app_healthcheck_ok(app, raw, state, monkeypatch):
    make_env(state, "hc-app")

    async def ok_fetch(url, *, timeout=10.0, max_bytes=1 << 25):
        return {"status": 200, "headers": {},
                "body": b"<html><head><title>My App</title></head></html>",
                "url": url, "truncated": False}

    payload, is_error = _healthcheck(app, raw, state, monkeypatch, ok_fetch)
    assert is_error is False
    assert payload["ok"] is True
    assert payload["status"] == 200
    assert payload["title"] == "My App"
    assert payload["error_class"] is None
    assert set(("status", "title", "ok", "error_class")) <= set(payload)


@pytest.mark.parametrize("label", [
    "http_error", "timeout", "unreachable", "redirect_blocked",
])
def test_app_healthcheck_error_classes(app, raw, state, monkeypatch, label):
    make_env(state, "hc-app")

    async def http_error(url, *, timeout=10.0, max_bytes=1 << 25):
        return {"status": 500, "headers": {}, "body": b"boom", "url": url,
                "truncated": False}

    async def timeout(url, *, timeout=10.0, max_bytes=1 << 25):
        raise TimeoutError("deadline")

    async def unreachable(url, *, timeout=10.0, max_bytes=1 << 25):
        raise OSError("connection refused")

    async def redirect_blocked(url, *, timeout=10.0, max_bytes=1 << 25):
        return {"status": 302,
                "headers": {"location": "https://evil.example/"},
                "body": b"", "url": url, "truncated": False}

    fetch = {"http_error": http_error, "timeout": timeout,
             "unreachable": unreachable,
             "redirect_blocked": redirect_blocked}[label]
    payload, is_error = _healthcheck(app, raw, state, monkeypatch, fetch)
    assert is_error is False  # a down app is data, not a tool error
    assert payload["ok"] is False
    assert payload["error_class"] == label


def test_app_healthcheck_same_host_redirect_followed(app, raw, state,
                                                     monkeypatch):
    make_env(state, "hc-app")

    async def redirect(url, *, timeout=10.0, max_bytes=1 << 25):
        if url.endswith("/"):
            return {"status": 302, "headers": {"location": "/app"},
                    "body": b"", "url": url, "truncated": False}
        return {"status": 200, "headers": {},
                "body": b"<title>Redirected</title>", "url": url,
                "truncated": False}

    payload, is_error = _healthcheck(app, raw, state, monkeypatch, redirect)
    assert payload["ok"] is True
    assert payload["title"] == "Redirected"


def test_app_healthcheck_no_domain(app, raw, state, monkeypatch):
    make_env(state, "hc-app")
    state.envs["hc-app"].domain = ""

    async def never(url, *, timeout=10.0, max_bytes=1 << 25):
        raise AssertionError("must not fetch without a domain")

    payload, is_error = _healthcheck(app, raw, state, monkeypatch, never)
    assert is_error is False
    assert payload["ok"] is False
    assert payload["error_class"] == "no_domain"


# --- the deploy-anything wizard end-to-end (FakeLLM) ------------------------


def test_deploy_rust_app_from_git_end_to_end(app, state, monkeypatch):
    """'deploy my Rust app from <git url>': detection chip -> runtime image
    -> healthcheck verify -> success card -> security review."""
    monkeypatch.setattr(mcp_tools, "http_fetch",
                        _zip_fetcher("rust", []))
    token = make_env(state, "existing-1")
    llm = FakeLLM(
        llm_flow("deploy_app", {
            "source": "https://github.com/acme/rust-demo.git",
            "env_name": "rust-demo", "cloudlets": 4, "ssl": False, "db": ""}),
        FINISH)
    agent_app = make_agent(app, llm=llm, verifier=FakeVerifier())

    events = run(drive_chat(
        agent_app, "conv-rust",
        "deploy my Rust app from https://github.com/acme/rust-demo.git",
        [{"kind": "ask", "answer": "Rust 1.80 (detected from Cargo.toml)"},
         {"kind": "confirm", "approved": True},
         {"kind": "card", "action": "cancel", "payload": {}}],
        scenario_headers(token)))
    assert_contract(events)

    # the stack step proposed the DETECTED stack as the primary chip
    asks = [p for name, p in events if name == "ask_user"]
    assert asks and asks[0]["options"][0] == \
        "Rust 1.80 (detected from Cargo.toml)"
    assert "detected from Cargo.toml" in asks[0]["options"][0]

    # the deploy routed through the tiered table: docker runtime image node
    creates = tool_calls(events, "env_create")
    assert creates and creates[0]["args"]["stack"] == "docker:rust:1.80-slim"
    deploys = tool_calls(events, "deploy_any")
    assert deploys and deploys[0]["args"]["runtime_image"] == "rust:1.80-slim"
    assert deploys[0]["args"]["port"] == 8080

    # verify chain: nodes_ready -> curl -> app_healthcheck
    names = [p["name"] for name, p in events if name == "tool_call"]
    assert "project_inspect" in names
    assert "op_result" in names
    assert "app_healthcheck" in names
    health_results = [p for name, p in events if name == "tool_result"
                      and "did not start" not in p["summary"]
                      and "is up" in p["summary"]]
    assert health_results and health_results[0]["ok"] is True

    # success card: Open app / Open dashboard / View logs
    success = cards(events, "success")
    assert success
    labels = [a["label"] for a in success[0]["actions"]]
    assert labels == ["Open app", "Open dashboard", "View logs"]

    # recap named the runtime image; security review is the last step
    confirms = [p for name, p in events if name == "confirm_action"]
    assert "rust:1.80-slim" in confirms[0]["summary"]
    assert cards(events, "security")
    assert events[-1][0] == "done"

    # the platform really changed
    env = state.envs["rust-demo"]
    assert env.status == "running"
    assert len(env.installations) == 1

    assert "changed nothing" not in texts(events)


def test_deploy_unknown_language_asks_runtime_image(app, state, monkeypatch):
    """Universal start guarantee: a marker-less source never errors out -
    the wizard asks 'Which runtime image should I use?' (free text allowed)."""
    monkeypatch.setattr(mcp_tools, "http_fetch",
                        _zip_fetcher("unknown", []))
    token = make_env(state, "existing-1")
    llm = FakeLLM(
        llm_flow("deploy_app", {
            "source": "https://example.com/exotic.zip",
            "env_name": "exotic-app", "cloudlets": 4, "ssl": False, "db": ""}),
        FINISH)
    agent_app = make_agent(app, llm=llm, verifier=FakeVerifier())

    events = run(drive_chat(
        agent_app, "conv-exotic", "deploy my app from "
        "https://example.com/exotic.zip",
        [{"kind": "ask", "answer": "myregistry.example.com/exotic:1"},
         {"kind": "confirm", "approved": True},
         {"kind": "card", "action": "cancel", "payload": {}}],
        scenario_headers(token)))
    assert_contract(events)

    asks = [p for name, p in events if name == "ask_user"]
    assert asks and asks[0]["question"] == "Which runtime image should I use?"
    assert "Deploy on a VPS node (bootstrap script)" in asks[0]["options"]

    deploys = tool_calls(events, "deploy_any")
    assert deploys and deploys[0]["args"]["runtime_image"] == \
        "myregistry.example.com/exotic:1"
    creates = tool_calls(events, "env_create")
    assert creates and creates[0]["args"]["stack"] == \
        "docker:myregistry.example.com/exotic:1"
    assert cards(events, "success")
    assert events[-1][0] == "done"
    assert len(state.envs["exotic-app"].installations) == 1
