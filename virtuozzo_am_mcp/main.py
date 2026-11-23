"""``virtuozzo-am-mcp`` CLI (argparse): login / install / serve."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Optional

from . import __version__
from .agents import (
    AGENTS,
    AGENT_GENERIC,
    AGENT_HERMES,
    InstallError,
    build_entry,
    detect_agent,
    hermes_one_liners,
    one_liners,
    write_agent_config,
)
from .config import ConfigError, load_config
from .login import LoginError, login_with_password, login_with_session, sso_instructions


# -- credential resolution ---------------------------------------------------

def resolve_credentials(host: Optional[str] = None,
                        session: Optional[str] = None,
                        *, home: Optional[Path] = None) -> tuple[str, str]:
    """host/session each resolve independently: flag > env > config file.

    Raises :class:`LoginError` when either is missing.
    """
    cfg: Optional[dict] = load_config(home)  # ConfigError propagates
    resolved_host = (host or os.environ.get("JELASTIC_HOST")
                     or (cfg or {}).get("host"))
    resolved_session = (session or os.environ.get("JELASTIC_SESSION")
                        or (cfg or {}).get("session"))
    if not resolved_host or not resolved_session:
        missing = "host" if not resolved_host else "session"
        raise LoginError(
            f"no platform {missing} — run `virtuozzo-am-mcp login "
            "--host <panel-url>` first (or pass --host/--session)")
    return str(resolved_host), str(resolved_session)


# -- serve -------------------------------------------------------------------

def run_serve(host: str, session: str, *, stdin=None, stdout=None,
              transport=None) -> None:
    """stdio MCP bridge -> platform API (same tool surface as ``mcp/``)."""
    try:
        from mcp.server import MCPServer
    except ImportError as exc:
        raise RuntimeError(
            "cannot import the bundled 'mcp' server package (it must sit next "
            "to virtuozzo_am_mcp/ on PYTHONPATH); if this is a source "
            "checkout, run from the repo root") from exc
    server = MCPServer(transport=transport)
    env = {"JELASTIC_HOST": host, "JELASTIC_SESSION": session}
    asyncio.run(server.run_stdio(stdin=stdin, stdout=stdout, env=env))


# -- subcommand handlers -----------------------------------------------------

def cmd_login(args, *, transport=None, home: Optional[Path] = None) -> int:
    host = args.host
    if args.session:
        result = asyncio.run(login_with_session(
            host, args.session, transport=transport, home=home))
        print(f"Logged in to {result['host']} (session verified).")
    elif args.email or args.password_stdin:
        email = args.email
        if not email:
            print("Email is required with --password-stdin", file=sys.stderr)
            return 2
        if args.password_stdin:
            password = sys.stdin.readline().rstrip("\n")
        else:
            import getpass
            password = getpass.getpass(f"Password for {email} at {host}: ")
        result = asyncio.run(login_with_password(
            host, email, password, transport=transport, home=home))
        print(f"Logged in to {result['host']} as {result['login']}.")
    else:
        # SSO / Keycloak browser flow.
        import getpass
        import webbrowser

        for line in sso_instructions(host):
            print(line)
        if not args.no_browser:
            webbrowser.open(host)
        try:
            token = getpass.getpass("Session token: ")
        except Exception:
            token = input("Session token: ")
        result = asyncio.run(login_with_session(
            host, token.strip(), transport=transport, home=home))
        print(f"Logged in to {result['host']} (session verified).")
    print(f"Session stored in {result['config_path']} (mode 0600).")
    return 0


def cmd_install(args, *, home: Optional[Path] = None) -> int:
    host, session = resolve_credentials(args.host, args.session, home=home)
    agent = args.agent if args.agent != "auto" else detect_agent(home=home)
    entry = build_entry(args.mode, host, session, mcp_url=args.url)
    output = Path(args.output) if args.output else None
    if agent == AGENT_GENERIC and output is None:
        output = Path("mcp-config.json")
    path = write_agent_config(agent, entry, home=home, output=output)

    print(f"Agent: {agent}" + (" (auto-detected)" if args.agent == "auto" else ""))
    print(f"MCP config written: {path}")
    print()
    if agent == AGENT_HERMES:
        for line in hermes_one_liners(args.mode, host, session, mcp_url=args.url):
            print(line)
    else:
        for line in one_liners(args.mode, host, session, mcp_url=args.url):
            print(line)
    if agent == AGENT_GENERIC:
        print(f"\nPaste the 'mcpServers' block from {path} into your client's "
              "MCP configuration.")
    if args.mode == "remote":
        print("\nRemote mode: no local bridge runs — the client talks straight "
              "to the hosted /mcp endpoint.")
    else:
        print("\nLocal mode: the agent spawns `virtuozzo-am-mcp serve`; the "
              "session is read from ~/.virtuozzo-am/config.json at startup.")
    return 0


def cmd_serve(args, *, stdin=None, stdout=None, transport=None,
              home: Optional[Path] = None) -> int:
    host, session = resolve_credentials(args.host, args.session, home=home)
    try:
        run_serve(host, session, stdin=stdin, stdout=stdout, transport=transport)
    except RuntimeError as exc:
        print(f"virtuozzo-am-mcp serve: {exc}", file=sys.stderr)
        return 1
    return 0


# -- parser ------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="virtuozzo-am-mcp",
        description="One-command MCP onboarding for the Virtuozzo Application "
                    "Manager (Jelastic/VAP): log in, connect your agent, or "
                    "run a local stdio MCP bridge.")
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser(
        "login", help="authenticate to the platform and store the session")
    p_login.add_argument("--host", required=True,
                         help="panel URL, e.g. https://<your-panel-host>")
    p_login.add_argument("--email",
                         help="platform login (password flow); omit for the "
                              "SSO browser flow")
    p_login.add_argument("--password-stdin", action="store_true",
                         help="read the password from stdin (never pass it as "
                              "an argument)")
    p_login.add_argument("--session",
                         help="use an existing dashboard session token "
                              "(Keycloak/SSO: log in in the browser, copy the "
                              "token, paste it here)")
    p_login.add_argument("--no-browser", action="store_true",
                         help="SSO flow: print instructions without opening a "
                              "browser")
    p_login.set_defaults(func=cmd_login)

    p_install = sub.add_parser(
        "install", help="detect your agent and write its MCP config")
    p_install.add_argument("--agent", choices=("auto",) + AGENTS,
                           default="auto",
                           help="target agent (default: auto-detect)")
    p_install.add_argument("--mode", choices=("local", "remote"),
                           default="local",
                           help="local stdio bridge (default) or hosted "
                                "remote endpoint")
    p_install.add_argument("--url",
                           help="remote mode: the hosted MCP endpoint, "
                                "e.g. https://assistant.example.com/mcp")
    p_install.add_argument("--output",
                           help="generic agent: where to write the "
                                "mcpServers JSON (default ./mcp-config.json)")
    p_install.add_argument("--host",
                           help="panel URL override (default: from "
                                "~/.virtuozzo-am/config.json)")
    p_install.add_argument("--session",
                           help="session token override (default: from "
                                "~/.virtuozzo-am/config.json)")
    p_install.set_defaults(func=cmd_install)

    p_serve = sub.add_parser(
        "serve", help="run the stdio MCP bridge to the platform API")
    p_serve.add_argument("--host",
                         help="panel URL (default: from "
                              "~/.virtuozzo-am/config.json)")
    p_serve.add_argument("--session",
                         help="session token override (default: from "
                              "~/.virtuozzo-am/config.json)")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv=None, *, transport=None, home: Optional[Path] = None,
         stdin=None, stdout=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.func is cmd_login:
            return args.func(args, transport=transport, home=home)
        if args.func is cmd_install:
            return args.func(args, home=home)
        return args.func(args, transport=transport, home=home,
                         stdin=stdin, stdout=stdout)
    except (LoginError, InstallError, ConfigError) as exc:
        print(f"virtuozzo-am-mcp: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
