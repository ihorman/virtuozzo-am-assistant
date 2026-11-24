# scripts

| File | Purpose |
|---|---|
| `package_extension.sh` | build `dist/virtuozzo-am-assistant-extension-v<version>.zip` from `extension/` (manifest.json at archive root; python3 zipfile — no zip binary needed) |
| `e2e_demo.sh` | live end-to-end demo against a real stand (VHC 8.15.1): throwaway `mcp-test-<ts>` environment → deploy sample app (HTTP 200 verified) → add DB node → vertical scale → read logs → agent-chat turn → explicit deletion listing → delete (with `passphrase=<envName>`) → confirmed gone. All phases are labeled `ok`/`fail` with a summary line. Verified green 2026-11-24: 12 ok / 0 failed. |
| `e2e_agent_chat.py` | the agent-chat phase as a standalone harness: drives one turn against the real platform through the FastAPI agent (in-process ASGI), asserts the SSE `tool_call` → `tool_result` → `done` contract |

Usage:

- `scripts/package_extension.sh [extension-src-dir] [output-dir]`
- `JELASTIC_SESSION=<token> [JELASTIC_PASSWORD=<pass>] [JELASTIC_HOST=<stand>] scripts/e2e_demo.sh [--keep]`
  — `JELASTIC_HOST` defaults to `https://app.vhc.virtuozzo.com`; `--keep`
  skips phase-7 deletion. Creates ONLY `mcp-test-*` environments and deletes
  them after an explicit listing. `JELASTIC_PASSWORD` is needed for the
  delete phase (the API requires it plus `passphrase=<envName>`).
- `python3 scripts/e2e_agent_chat.py --host <stand> --session <token> --env <name>`
  — one read-only agent-chat turn against the live platform.

Live API shapes these scripts rely on: [`docs/live-api-notes.md`](../docs/live-api-notes.md)
("Live e2e-verified shapes"). Final run log of the green delivery:
kept with the task artifacts (t7-e2e-final-run.log).
