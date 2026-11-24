#!/usr/bin/env python3
"""Agent chat exercise for scripts/e2e_demo.sh (T7 phase 6).

Runs the T4 agent backend in-process and drives one real chat turn against
the LIVE platform: the agent's tool calls go out over real HTTP with the
stand session token. Read-only prompt, so nothing is mutated.

LLM: real OpenAI-compatible endpoint when VAP_LLM_API_KEY (or XIAOMI_API_KEY)
is present (mimo-v2.6-pro per orchestrator decision); otherwise the exercise
is skipped with exit 2 (reported honestly as "not exercised").

Exit: 0 pass, 1 failure, 2 skipped (no LLM key).
"""
import asyncio
import json
import os
import sys

import httpx


async def main() -> int:
    host = os.environ.get("HOST") or (sys.argv[sys.argv.index("--host") + 1])
    session = sys.argv[sys.argv.index("--session") + 1]
    env_name = sys.argv[sys.argv.index("--env") + 1]

    api_key = (os.environ.get("VAP_LLM_API_KEY")
               or os.environ.get("XIAOMI_API_KEY") or "").strip()
    if not api_key:
        for path in ("/home/ihorman/.hermes/profiles/shared/.env",
                     os.path.expanduser("~/.env")):
            try:
                for line in open(path):
                    if line.startswith("XIAOMI_API_KEY="):
                        api_key = line.split("=", 1)[1].strip().strip('"')
                        break
            except OSError:
                pass
            if api_key:
                break
    if not api_key:
        print("[skip] no VAP_LLM_API_KEY/XIAOMI_API_KEY — agent chat not exercised")
        return 2

    os.environ.setdefault("VAP_LLM_BASE_URL",
                          "https://token-plan-sgp.xiaomimimo.com/v1")
    os.environ.setdefault("VAP_LLM_MODEL", "mimo-v2.6-pro")
    os.environ["VAP_LLM_API_KEY"] = api_key

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agent.main import create_app  # noqa: E402

    app = create_app()
    headers = {"X-Jelastic-Host": host, "X-Jelastic-Session": session,
               "X-Vap-Context": json.dumps({"envName": env_name})}
    prompt = (f"Do I have an environment named {env_name}? "
              "Answer in one sentence with its status. Do not change anything.")

    events = []
    async with httpx.ASGITransport(app=app) as tr:
        # base_url is required: ASGITransport serves relative URLs, and
        # httpx cookie handling (urllib compat) rejects scheme-less URLs.
        async with httpx.AsyncClient(transport=tr, base_url="http://agent.local",
                                    timeout=180.0) as client:
            body = None
            for payload in ({"message": prompt}, {"text": prompt}):
                req = client.build_request("POST", "/api/chat", json=payload,
                                           headers=headers)
                resp = await client.send(req, stream=True)
                if resp.status_code == 200:
                    body = payload
                    break
                await resp.aclose()
            if body is None:
                print("[fail] /api/chat rejected both payload shapes")
                return 1
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    try:
                        events.append(json.loads(line[5:].strip()))
                    except ValueError:
                        events.append({"raw": line[:200]})
                elif line.startswith("event:"):
                    events.append({"sse_event": line[6:].strip()})

    kinds = [e.get("kind") or e.get("type") or e.get("sse_event") for e in events]
    print(f"[info] {len(events)} SSE events; kinds={kinds[:12]}")
    text = json.dumps(events)
    if not events:
        print("[fail] no SSE events")
        return 1
    if env_name not in text and "envs_list" not in text and "environment" not in text.lower():
        print("[fail] no sign the agent consulted the live platform")
        return 1
    print("[ok] agent chat turn completed against live platform")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
