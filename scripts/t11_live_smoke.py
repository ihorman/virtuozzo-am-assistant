#!/usr/bin/env python3
"""T11 live smoke: exercise the MCP tool layer READ-ONLY against real VHC
8.15.1 (app.vhc.virtuozzo.com).

Covers the tools behind the verified live shapes: env_info (getenvinfo,
numeric status normalization), env_stats (getsumstat), logs_read listing
(getlogs), logs_read content (readlog -> {linescount, body}), logs_read
list_dirs (getlogslist). No mutations.

Usage: scripts/t11_live_smoke.py [env_name]
       (default env: the mcp-test-* env created for probing)

Session token: /home/ihorman/.hermes/profiles/shared/cache/scratch/vhc_session.txt
"""

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, "/home/ihorman/workspace/vap-copilot")

from mcp.tools import AuthConfig, execute_tool  # noqa: E402

HOST = "https://app.vhc.virtuozzo.com"
TOKEN_FILE = pathlib.Path(
    "/home/ihorman/.hermes/profiles/shared/cache/scratch/vhc_session.txt")

PASS = 0
FAIL = 0


def ok(msg):
    global PASS
    PASS += 1
    print(f"[OK ] {msg}")


def bad(msg):
    global FAIL
    FAIL += 1
    print(f"[FAIL] {msg}")


async def tool(auth, name, args):
    payload, is_error = await execute_tool(name, args, auth, timeout=120.0)
    return payload, is_error


async def main(env_name):
    token = TOKEN_FILE.read_text().strip()
    auth = AuthConfig(host=HOST, session=token)
    print(f"=== T11 live smoke: host={HOST} env={env_name} ===")

    # -- env_info: flat getenvinfo + numeric-status normalization ----------
    payload, is_error = await tool(auth, "env_info", {"env_name": env_name})
    if is_error:
        bad(f"env_info: {payload}")
        return
    env = payload.get("env") or {}
    nodes = env.get("nodes") or []
    if env.get("statusName") == "running" and env.get("status") == 1:
        ok(f"env_info: status 1 normalized to 'running' "
           f"(domain={env.get('domain')})")
    else:
        bad(f"env_info: unexpected status {env.get('status')}/"
            f"{env.get('statusName')}")
    if nodes:
        ok(f"env_info: {len(nodes)} node(s), first id="
          f"{nodes[0].get('id')} type={nodes[0].get('nodeType')}")
    else:
        bad("env_info: no nodes returned")

    # -- env_stats: getsumstat rows (sum row filtered, names bridged) ------
    payload, is_error = await tool(auth, "env_stats", {"env_name": env_name})
    if is_error:
        bad(f"env_stats: {payload}")
    else:
        rows = payload.get("nodes") or []
        if rows and all(str(r.get("nodeid")) != "sum" for r in rows) \
                and {"cpu", "mem", "disk"} <= set(rows[0]) \
                and rows[0].get("id") is not None:
            ok(f"env_stats: {len(rows)} stat row(s), "
               f"nodeid={rows[0].get('nodeid')} id={rows[0].get('id')} "
               f"cpu={rows[0].get('cpu')} mem={rows[0].get('mem')} "
               f"ram={rows[0].get('ram')}")
        else:
            bad(f"env_stats: unexpected rows {json.dumps(rows)[:200]}")

    # -- logs_read: getlogs keyword/file listing --------------------------
    payload, is_error = await tool(auth, "logs_read", {"env_name": env_name})
    log_path = None
    if is_error:
        bad(f"logs_read listing: {payload}")
    else:
        logs = payload.get("logs") or []
        if logs and logs[0].get("files"):
            log_path = logs[0]["files"][0].get("path")
            ok(f"logs_read listing: {payload.get('count')} keyword group(s), "
               f"first file={log_path}")
        else:
            bad(f"logs_read listing: unexpected shape "
                f"{json.dumps(logs)[:200]}")

    # -- logs_read: readlog content {linescount, body} --------------------
    if log_path:
        payload, is_error = await tool(auth, "logs_read",
                                       {"env_name": env_name, "path": log_path,
                                        "line": 5})
        if is_error:
            bad(f"logs_read content: {payload}")
        elif payload.get("linescount") is not None and \
                isinstance(payload.get("body"), str):
            ok(f"logs_read content: linescount={payload['linescount']} "
               f"body={len(payload['body'])} chars (framed)")
        else:
            bad(f"logs_read content: unexpected shape "
                f"{json.dumps(payload)[:200]}")

    # -- logs_read: getlogslist dirs -------------------------------------
    payload, is_error = await tool(auth, "logs_read",
                                   {"env_name": env_name, "list_dirs": True})
    if is_error:
        bad(f"logs_read list_dirs: {payload}")
    elif payload.get("dirs"):
        ok(f"logs_read list_dirs: {payload.get('count')} entr(ies)")
    else:
        bad(f"logs_read list_dirs: unexpected shape "
            f"{json.dumps(payload)[:200]}")

    print(f"=== SUMMARY: {PASS} ok, {FAIL} failed ===")


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else \
        pathlib.Path("/tmp/smoke_env_name.txt").read_text().strip().split("=")[-1]
    asyncio.run(main(name))
    sys.exit(1 if FAIL else 0)
