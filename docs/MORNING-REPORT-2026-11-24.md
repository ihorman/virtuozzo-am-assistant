# Morning report — Virtuozzo Application Management Assistant (virtuozzo-am-assistant)

**Date:** 2026-11-24, ~03:30 EET · **For:** Ihor · **Repo:** `~/workspace/vap-copilot` (all commits **local only** — nothing pushed)

---

## 1. What was built

An end-user AI assistant for the Virtuozzo Application Manager (V/AM, ex-Jelastic/VAP), in three surfaces plus tooling — everything in one repo, zero platform-side changes:

| Component | Where | Commit(s) | What it does |
|---|---|---|---|
| **MCP server** | `mcp/` | `2d78cb7`, `8ae95c4` | The platform REST API as **21 confirm-gated MCP tools** (JSON-RPC + Streamable HTTP at `/mcp` + stdio). Auth passthrough (caller's own session, never stored), `confirm: true` gate on every mutation, `env_delete` extra gates, SSRF/host-allowlist/token-pinning confinement layer (`mcp/security.py`), rate limits, secret masking, audit trail. |
| **Agent backend** | `agent/` | `bb93439` | FastAPI: `POST /api/chat` (SSE), `/api/answer`, `/api/context`, `/api/audit`, `/healthz`. Guided flows (deploy / add DB / scale / troubleshoot) with clarifying questions, confirm cards, deploy verification (curl the deployed URL), error→diagnose→guarded auto-fix, security-review card. LLM is env-configured OpenAI-compatible (`VAP_LLM_*`) — no provider hardcoded. |
| **Browser extension** | `extension/` | `2129bed` | Chrome MV3: floating **AI Assistant** button on dashboard pages, streaming chat side panel (markdown, wizard dots, ask chips, typed-confirm destructive gate, progress/success/error/security/audit cards), options page (agent URL, host, session-token fallback, diagnostics), bundled fonts. All agent traffic through an origin-scoped background fetch proxy. |
| **Onboarding CLI** | `virtuozzo_am_mcp/`, `packaging/npm/` | `8a9ee87`, `7103806` | `virtuozzo-am-mcp` (`npx` or `python3 -m`): `login` (SSO browser flow / password / `--session`), `install` (detects Claude Code / Cursor / Hermes / generic and writes its MCP config in one command), `serve` (local stdio bridge). |
| **Mock platform** | `mock/` | `bd2a722` | Mock Jelastic REST API + mock dashboard page — the whole suite is network-free against it. |
| **Deploy kit** | `deploy/` | `a1e36d3` | Dockerfile (non-root, healthcheck, key never baked in), combined ASGI serve (`/mcp` + `/api/*` + `/healthz`), JPS manifest (self-hosting on the platform), Caddy snippet + systemd unit for `hermes.bannoe.it`, extension packaging script. |
| **Docs** | `docs/`, `README.md` | `c3b3bc7` … this pass | Plan, API contract (v1.1 + additive v1.2 card actions), usage guide, design system (DESIGN.md), security threat model, **live API notes with the e2e-verified shapes**. |
| **Live e2e harness** | `scripts/e2e_demo.sh`, `scripts/e2e_agent_chat.py` | this pass | 7-phase live demo run (see §3) + standalone agent-chat turn harness. |

## 2. How to install the browser extension

1. `scripts/package_extension.sh` → `dist/virtuozzo-am-assistant-extension-v<version>.zip` (or use the `extension/` directory directly).
2. Chrome/Chromium 116+ → `chrome://extensions` → enable **Developer mode** → **Load unpacked** → pick `extension/`.
3. Done — zero configuration needed: the default agent endpoint is the hosted one (`https://hermes.bannoe.it/am-assistant`) and the session token is captured from the logged-in dashboard automatically. Options page: change agent URL (e.g. `http://127.0.0.1:8787` for offline dev with `python3 -m tests.mock_agent`), set platform host, or paste the session token manually if auto-capture fails.

## 3. How to connect external MCP clients

Every client needs the MCP endpoint + your platform session (`X-Jelastic-Host` = panel URL, `X-Jelastic-Session` = dashboard session token). Full per-client configs (Claude Desktop, Cursor, Hermes, generic JSON) are in **README → "Connect an external MCP client"**; the one-command path:

```bash
npx virtuozzo-am-mcp login --host https://<your-panel-host>
npx virtuozzo-am-mcp install          # detects your agent, writes its config
```

Hosted mode: `https://<assistant-host>/mcp` (e.g. `https://hermes.bannoe.it/am-assistant/mcp`) with the two headers; local mode: `npx virtuozzo-am-mcp serve` (stdio bridge, session from `~/.virtuozzo-am/config.json`, mode 0600). Read tools work immediately; mutating tools only with `confirm: true` after the user confirmed the exact action — any client that auto-ignores the gates is unsafe to connect.

## 4. Verified ✅ (with evidence)

1. **Test suite: 237 passed** — network-free pytest (socket guard), re-run green **today at the current tree** (39.8s).
2. **Live e2e on a real stand** — `scripts/e2e_demo.sh` against `app.vhc.virtuozzo.com` (VHC 8.15.1): **12 ok / 0 failed**. Phases: create `mcp-test-<ts>` env (nginxphp-dockerized) → deploy Spoon-Knife sample zip → **HTTP 200 verified** → add mariadb DB node (changetopology) → vertical scale → read logs (getlogs/readlog) → **real agent-chat turn against the live platform** (SSE `tool_call`→`tool_result`→`done`; the agent consulted real env state, read-only) → explicit deletion listing → `deleteenv` → confirmed gone. Run log delivered as task artifact `t7-e2e-final-run.log`.
3. **Agent chat contract holds live** — exercised twice against the real platform (10 and 8 SSE events, contract order intact).
4. **Extension** — headless-Chromium smoke against mock agent + mock dashboard: FAB renders, full chat roundtrip (chip → ask → Confirm → progress → success card) driven purely via on-screen affordances; 19-state DESIGN.md screenshot set (dark + light) captured and reviewed. One caveat: smoke runs Chromium `--single-process` (container pids cap 400).
5. **Agent backend** — 12 flow tests with a FakeLLM drive each wizard end-to-end vs the mock and assert the SSE contract exactly; plus a real-socket smoke (uvicorn agent + mock platform, 33 SSE events).
6. **Security confinement** — host allowlist + token pinning (no redirect following), request validation, secret masking, rate limits, read-only mode: unit-tested (T10).
7. **Cleanup discipline** — every `mcp-test-*` env created live (4 total, incl. 2 prior leftovers) was listed explicitly and deleted; stand verified clean (only the out-of-scope Constructor Fabric envs remain).
8. **Live API shapes** — captured and written up in `docs/live-api-notes.md` ("Live e2e-verified shapes"): form-encoded POST only, `deployarchive` wants `fileUrl`+`fileName`, `changetopology` env descriptor + nodes JSON, `getsumstat` (not `getenvstat`), `readlog` for content, numeric `status`, `deleteenv` needs `passphrase=<envName>`, result-code table.

## 5. NOT verified ⚠️ (explicit)

1. **MCP tool layer against live VHC** — the 21-tool layer is green against the mock + unit tests, but live VHC uses different method names/shapes (`getenvstat`→`getsumstat`, `getenvvariables`→`getcontainerenvvars*`, deploy `url`→`fileUrl`, `deleteenv` passphrase). The fixes are **in progress on card T11** (`t_8657a8b8`) with the full verified-shapes spec. Until it lands, treat MCP tool calls against a live stand as unreliable; the e2e harness and agent chat (which use verified REST shapes) are the live-proven paths.
2. **Hosted deployment** (`https://hermes.bannoe.it/am-assistant`) — deploy kit is ready (Dockerfile / systemd unit / Caddy snippet / JPS), but the service is **not yet deployed or curl-verified on the host**. The extension's default agent URL points there, so it needs deploying before the extension works out of the box against the real backend.
3. **Stand auth end-to-end** — live access works with a **browser session token** (`vhc_session.txt` in scratch; **may expire at any time**). The `virtuozzo-am-mcp login` SSO/password flow has **not** been exercised against the live stand (password grant is disabled for SSO accounts — result 701 — so the SSO flow is the only candidate and is untested there).
4. **Extension against the real dashboard** — smoke-tested against the mock dashboard only; FAB/session-capture on a real VHC dashboard page not yet exercised.
5. **npx package** — `packaging/npm/` builds locally; not published to npm, and the `npx virtuozzo-am-mcp` one-liner therefore doesn't resolve yet (works as `python3 -m virtuozzo_am_mcp`).
6. **JPS self-hosting manifest** — authored, not installed on a stand.
7. **Nothing is pushed** — all commits are local-only per the test-first rule.

## 6. Open items (need your input / next steps)

1. **Stand auth (blocker for durable demos):** the session token in scratch expires. Decide: (a) run `virtuozzo-am-mcp login` SSO flow once it's verified against the stand, (b) provide a longer-lived token, or (c) accept manual re-login for demos.
2. **LLM key for the demo agent:** `VAP_LLM_API_KEY` must be provisioned at deploy time (nothing paid is wired in-repo, by design). Endpoint config is ready (`VAP_LLM_BASE_URL` / `VAP_LLM_MODEL`, verified with mimo-v2.6-pro during development).
3. **T11** (`t_8657a8b8`, in progress): MCP tool-layer live-compat fixes — method renames, `fileUrl` mapping, `changetopology` descriptor, `deleteenv` passphrase, status normalization.
4. **Uncommitted WIP in the tree** (green with the suite, but uncommitted): `mcp/jelastic_client.py` + `mcp/tools.py` (T11 will commit these), `mock/server.py` (form-encoding + region fixes — **needs a follow-up commit owner**), `scripts/e2e_live.py` (superseded probe harness; scratch, not part of the delivered e2e). 
5. **Deploy to `hermes.bannoe.it`** per `deploy/` procedure (Caddyfile there carries many reverse proxies — full review + backup + `caddy validate` mandatory) and then curl-verify `/healthz` and `/am-assistant/mcp`.
6. **First real-dashboard pass** of the extension (FAB + session capture on a live VHC page).

## 7. Where things are

- User guide: `docs/USAGE.md` (extension install §1, MCP connect §2, self-hosting §3)
- README: quickstart, per-client MCP configs, deploy options, configuration reference, status summary
- Live API shapes: `docs/live-api-notes.md`
- Security threat model: `docs/SECURITY.md`
- e2e harness usage: `scripts/README.md`
