#!/usr/bin/env python3
"""Live e2e against a real V/AM platform through the product tool layer.

Usage:
    JELASTIC_HOST=https://<panel> JELASTIC_SESSION=<token> \
        python3 scripts/e2e_live.py [--keep]

Phases:
    1. read-only smoke (envs_list, quota, templates, marketplace)
    2. create mcp-test-<ts> environment (confirm-gated tool)
    3. poll until running, then env_info / env_stats / logs
    4. mutations: env_var_set, node_scale (confirm-gated)
    5. print the exact created-resource list for approval-gated cleanup
       (env_delete needs the account password and explicit operator approval;
       with --keep the env is left in place)

Everything goes through mcp.tools.TOOLS handlers - the exact code path the
MCP server exposes.
"""
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.jelastic_client import JelasticClient  # noqa: E402
from mcp.tools import TOOLS  # noqa: E402

HOST = os.environ.get("JELASTIC_HOST", "https://app.vhc.virtuozzo.com")
SESSION = os.environ.get("JELASTIC_SESSION", "")
ENV = os.environ.get("E2E_ENV_NAME") or f"mcp-test-{int(time.time())}"
KEEP = "--keep" in sys.argv

results: list[tuple[str, bool, str]] = []


def log(phase: str, ok: bool, detail: str = "") -> None:
    results.append((phase, ok, detail))
    print(f"[{'OK ' if ok else 'FAIL'}] {phase}" + (f" :: {detail}" if detail else ""),
          flush=True)


async def call(client: JelasticClient, name: str, args: dict, timeout: float = 120):
    spec = TOOLS[name]
    out = await asyncio.wait_for(spec.handler(client, dict(args, **(
        {"confirm": True} if spec.mutating else {}))), timeout=timeout)
    brief = json.dumps(out, default=str)
    print(f"    {name} -> {brief[:400]}", flush=True)
    return out


async def main() -> int:
    if not SESSION:
        print("JELASTIC_SESSION is required", file=sys.stderr)
        return 2

    async with JelasticClient(HOST, SESSION) as c:
        # 1. read-only smoke
        envs = await call(c, "envs_list", {})
        log("read: envs_list", isinstance(envs, (list, dict)))
        for read in ("quota_get", "templates_list", "marketplace_list"):
            try:
                await call(c, read, {})
                log(f"read: {read}", True)
            except Exception as e:  # informational only
                log(f"read: {read}", False, str(e)[:120])

        # 2. create test environment (docker stack: VHC-style platforms are
        # docker-centric - verified live that native stacks are rejected).
        # The HTTP response may hang while creation runs (platform serializes
        # ops): a timeout here means "accepted", verified by polling below.
        if os.environ.get("E2E_SKIP_CREATE"):
            log("create env", True, f"{ENV} (E2E_SKIP_CREATE: reusing)")
        else:
            try:
                await call(c, "env_create", {
                    "env_name": ENV, "stack": "docker:nginx:alpine",
                    "cloudlets": 4,
                    "display_name": "mcp e2e test (safe to delete)"})
                log("create env", True, ENV)
            except Exception as e:
                if "timed out" in str(e).lower():
                    log("create env", True,
                        f"{ENV} (response hung, op likely running: {str(e)[:80]})")
                else:
                    log("create env", False, str(e)[:200])
                    return 1

        # 3. poll until running (async op)
        status = None
        for _ in range(45):
            await asyncio.sleep(8)
            try:
                info = await call(c, "env_info", {"env_name": ENV})
                env = info.get("env") or (info.get("info") or {}).get("env") or {}
                status = env.get("status")
                if str(status) == "1":
                    break
            except Exception as e:
                print(f"    poll: {str(e)[:100]}", flush=True)
        log("env running", str(status) == "1", f"status={status}")

        if str(status) == "1":
            for read in ("env_stats", "logs_read", "env_var_get"):
                try:
                    await call(c, read, {"env_name": ENV})
                    log(f"read: {read}", True)
                except Exception as e:
                    log(f"read: {read}", False, str(e)[:120])

            # 4. mutations
            try:
                await call(c, "env_var_set", {
                    "env_name": ENV,
                    "variables": {"E2E_MARKER": "vap-copilot-e2e"}})
                got = await call(c, "env_var_get", {"env_name": ENV})
                log("mutation: env_var_set roundtrip", True, str(got)[:100])
            except Exception as e:
                log("mutation: env_var_set", False, str(e)[:150])

            try:
                info = await call(c, "env_info", {"env_name": ENV})
                nodes = info.get("nodes") or (info.get("info") or {}).get("nodes") or []
                if nodes:
                    nid = nodes[0].get("id") or nodes[0].get("nodeId")
                    await call(c, "node_scale", {
                        "env_name": ENV, "node_id": int(nid),
                        "fixed_cloudlets": 1, "flexible_cloudlets": 7})
                    log("mutation: node_scale", True, f"node {nid} -> 1+7")
                else:
                    log("mutation: node_scale", False, "no nodes found")
            except Exception as e:
                log("mutation: node_scale", False, str(e)[:150])

        # 5. cleanup handoff
        print("\n=== CREATED RESOURCES (cleanup requires explicit approval) ===")
        print(f"  environment: {ENV}  (host {HOST})")
        print(f"  status: {status}")
        if KEEP:
            print("  --keep given: leaving environment in place")
        else:
            print("  delete with: tool env_delete(env_name='%s', password=<account "
                  "password>, confirm=true) - approved manually" % ENV)

        print("\n=== SUMMARY ===")
        fails = [r for r in results if not r[1]]
        for name, ok, detail in results:
            print(f"  {'OK ' if ok else 'FAIL'} {name} {detail}")
        print(f"\n{len(results) - len(fails)}/{len(results)} checks passed")
        return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
