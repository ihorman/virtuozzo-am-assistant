# virtuozzo-am-assistant — Implementation Plan

**virtuozzo-am-assistant**: an end-user AI assistant for the Virtuozzo
Application Manager (V/AM, formerly Jelastic / Virtuozzo Application Platform).
Three deliverables in one repo:

1. **MCP server** — exposes the platform REST API as MCP tools, acting strictly
   with the end user's own rights.
2. **Agent backend** — chat + guided flows (deploy app, add database, scale,
   troubleshoot) with clarifying questions and action confirmations.
3. **Browser extension (Chrome MV3)** — the "widget": floating assistant button
   on the dashboard + Side Panel chat. Zero platform changes.

Testing constraint: **do not touch the existing system**. All live tests run
against the demo/VHC stand using throwaway `mcp-test-*` environments that are
explicitly listed and deleted afterwards. The agent service itself deploys as
its own environment (or on hermes.bannoe.it), never into platform components.

---

## 1. Architecture

```
+---------------------+        SSE chat        +----------------------+
|  Browser extension  | <--------------------> |   agent/ (FastAPI)   |
|  (MV3, side panel)  |                        |  LLM loop + flows    |
+----------+----------+                        +----------+-----------+
           | session token + page context                  | in-process tool calls
           v                                               v
+---------------------+      REST (per-user    +----------------------+
|  Jelastic dashboard |      session token)    |   mcp/ (MCP server)  |
|  (untouched)        | <--------------------> |  tools = API wrapper |
+---------------------+                        +----------+-----------+
                                                          | session param per call
                                                          v
                                              +------------------------+
                                              | Jelastic / VAP REST API|
                                              | (demo / VHC / any)     |
                                              +------------------------+
```

- **mcp/** is also exposed standalone (Streamable HTTP `/mcp`, plus stdio mode)
  so external MCP clients (Claude Desktop, Cursor, Hermes) can connect with
  their own session token. Same feature, "bring your own agent".
- The platform enforces user rights natively: every API call carries the user's
  session token. The assistant never stores passwords.

### Auth model

| Consumer | Credential flow |
|---|---|
| Extension -> agent | `X-Jelastic-Host` + `X-Jelastic-Session` headers (token captured from the logged-in dashboard) |
| External MCP client | same two headers on `/mcp`, or `JELASTIC_HOST`/`JELASTIC_SESSION` env for stdio |
| Agent -> platform | passthrough of the caller's token on every REST call (stateless) |

Session expiry (~30 min inactivity) is handled by surfacing a clear
"session expired, re-open the dashboard" error to the UI.

### Safety rails (end-user product, mandatory)

- Read tools: free.
- Mutating tools: the agent must call `confirm_action(summary)` and get a UI
  confirm before executing (extension renders Confirm/Cancel buttons).
- `env_delete` additionally requires the account password parameter per API
  (the extension asks the user to type it into the request; it is forwarded
  once, never stored) and a `confirm=true` argument.
- Every mutating tool is idempotent-checked where possible (e.g. delete of a
  missing env -> clean error, not a crash).

---

## 2. MCP tool surface (v1)

Built against the 310-method catalog (`~/workspace/files/jelastic-complete-guide.md`).
v1 wraps the ~22 methods that cover the end-user flows:

| Tool | REST method | Notes |
|---|---|---|
| `envs_list` | `environment/control/rest/getenvs` | list user's environments |
| `env_info` | `getenvinfo` | topology, nodes, status, domains |
| `env_stats` | `getenvstat` | CPU/RAM/disk per node |
| `env_create` | `createenvironment` | wizard-shaped params (stack, db, cloudlets) |
| `env_start` / `env_stop` / `env_sleep` | `startenv`/`stopenv`/`sleepenv` | |
| `env_delete` | `deleteenv` | gated: confirm + password passthrough |
| `node_scale` | `setcloudletscountbyid` | vertical scaling |
| `topology_change` | `changetopology` | horizontal scaling / add node groups |
| `deploy_archive` | `deployment/rest/deployarchive` | archive URL or upload |
| `deploy_jps` | `marketplace/jps/rest/install` | marketplace / JPS manifest URL |
| `db_add` | `changetopology` (db node group) | engine/version/creds/cloudlets |
| `logs_read` | `getlogs` | tail node/app logs |
| `env_var_get` / `env_var_set` | `environment/control/rest/getenvvariables`... | |
| `ssl_letsencrypt` | `environment/security/rest/...` | gated |
| `security_check` | composite (firewall, sslstate, endpoints, env-var scan) | in-flow security review; masks secret values |
| `audit_list` | — (assistant's own action log) | compliance: what changed, when, result |
| `marketplace_list` | `marketplace/app/rest/getcategories`... | curated app catalog for the deploy wizard |
| `templates_list` | `environment/control/rest/getcontainers`... | stacks/engines available |
| `quota_get` | `billing/quota/rest/getquotas` | "in your limits" awareness |
| `op_result` | — | poll async ops; Jelastic ops are fire-and-forget |

Known platform behaviors baked into the client: async ops (never conclude
failure from a timeout), `deleteenv` needs `password`, session expiry,
`2212` = deletion already running, per-environment operation serialization.

### One-command MCP connect (product requirement)

Connecting the assistant to the user's OWN agent must be one command:

```
npx virtuozzo-am-mcp login --host https://<panel>   # SSO browser flow or password
npx virtuozzo-am-mcp install                        # detects agent, writes config
```

`install` detects Claude Code / Cursor / Hermes / generic JSON-RPC client and
writes the MCP config; it also prints the manual one-liner (remote mode: a
single `mcp add --transport http` URL + `X-Jelastic-Session` header). The same
tool surface is available hosted at `https://<assistant-host>/mcp` for
zero-install setups, and via `virtuozzo-am-mcp serve` as a local stdio bridge.

## 3. Agent backend

FastAPI app (`agent/main.py`):

- `POST /api/chat` — SSE stream. LLM loop (OpenAI-compatible endpoint via env
  `VAP_LLM_BASE_URL` / `VAP_LLM_API_KEY` / `VAP_LLM_MODEL`; **no hardcoded
  provider** — the paid key is a deployment-time decision) with MCP tools
  called in-process.
- `GET /api/context` — echo of the captured dashboard context (current env).
- `GET /healthz` — liveness + configured provider (key never echoed).

**Guided flows** — deterministic state machines the LLM drives; each step asks
one clarifying question with options (the "наводящие вопросы"):

1. **Deploy app**: source (marketplace / VCS+archive URL / JPS) -> target env
   (existing or new -> name check) -> topology (stack + optional DB) ->
   resources (cloudlet presets: Dev 4 / Prod 12...) -> domain/SSL -> RECAP ->
   confirm -> execute -> poll -> verify (curl the env URL) -> success report
   with links.
2. **Add database**: engine -> version -> resources -> credentials (generated
   or provided) -> attach to env -> connection string output.
3. **Scale / troubleshoot**: read stats -> interpret -> propose -> confirm ->
   apply -> re-read stats.
4. **Anything else**: direct tool use with confirmation for mutations.
5. **Error diagnosis & auto-repair**: on ANY platform error or failed step the
   agent must not dead-end. It gathers evidence (logs, env/node stats, state
   around the failing resource), classifies the failure (quota, DNS/SSL, OOM,
   disk, port conflict, expired cert, failed JPS action, expired session...),
   explains the cause in plain language and proposes an **auto-fix plan** as a
   one-click primary action. Repair runs guarded: plan recap -> Confirm ->
   apply -> verify -> success card, or an honest "couldn't fix, here is what I
   tried and what to do next".
6. **Security & compliance in every flow** (never bolted on):
   - Deploy wizard ends with a **Security review** step: secrets-looking env
     vars flagged (values masked), public DB/ports exposure checked, SSL
     status, firewall state, outdated image notice. Findings render as a
     security card with one-click fixes (enable SSL, close exposure, rotate).
   - **Auto-fix guardrails**: a repair may never disable or weaken security
     (firewall, SSL, auth), never open new public endpoints, never widen
     permissions — such steps require their own explicit confirmation.
   - **Audit trail**: every mutation is recorded (timestamp, tool, target,
     result) and available via `GET /api/audit` / an "What I changed" card —
     full traceability for compliance; no silent changes, ever.
   - **Secrets hygiene**: passwords/tokens never stored server-side, never
     logged, always masked in tool results and cards (contract rule).
   - Least privilege by design: the assistant carries only the end user's own
     platform rights (session passthrough) — nothing more.

Clarifying-question protocol: tool `ask_user(question, options[], allow_free)`
renders as choice chips in the extension; free text always allowed.

## 4. Browser extension (Chrome MV3)

```
extension/
  manifest.json          # permissions: sidePanel, cookies, activeTab, storage
                         # host_permissions: <panel origin>, <agent origin>
  background.js          # service worker: settings, cookie read, fetch proxy
  sidepanel/             # chat UI (streams SSE from agent), confirm buttons,
                         # ask_user chips, markdown rendering
  content/               # FAB "AI Assistant" on dashboard pages
                         # context capture (env name from DOM/URL)
  options/               # agent URL, panel host, session diagnostics
  lib/api.js             # shared fetch/SSE helpers
```

- No platform changes; `Load unpacked` install for the demo.
- Session token: content/background reads the dashboard's session storage
  (Jelastic keeps the token in the page; exact capture is verified against the
  live stand during e2e — fallback: options page "paste session token").
- Works on any stand (demo, VHC, production customer stands) since it is
  origin-configurable.

## 5. Mock platform (`mock/`)

A small FastAPI/http.server that implements the used REST subset faithfully:
auth (701/501 result codes), getenvs/createenv/getenvinfo/getenvstat,
topology ops, deploy, JPS install, deleteenv (with 2212 semantics), logs,
variables, async status transitions. Used for:

- pytest unit/integration tests (client, MCP tools, agent flows with FakeLLM),
- extension development (mock dashboard page served locally),
- offline demo if the stand stays unreachable.

## 6. Testing plan

1. **Unit**: JelasticClient request shapes + result-code handling (pytest vs mock).
2. **MCP**: tool schemas, auth passthrough, gating on destructive tools.
3. **Agent flows**: FakeLLM script drives each wizard end-to-end vs mock;
   assert clarifying questions asked, confirmation required, verify step runs.
4. **Extension smoke**: open mock dashboard page in headless Chromium, assert
   FAB renders, side panel opens, chat roundtrip with agent + mock.
5. **Live e2e (stand)**: `scripts/e2e_demo.sh` — create `mcp-test-<ts>` env,
   deploy a sample app, add a DB node, scale, read logs, then **list everything
   created and delete it** (explicit deletion listing before acting). Requires
   working credentials/session token (currently blocked: see Risks).

## 7. Deployment kit

- **Target: hermes.bannoe.it** — agent + MCP served behind the existing Caddy
  (path route `/am-assistant/*` -> local uvicorn port; Caddyfile is shared and
  heavily proxied: full review + backup before the single-block addition,
  then `caddy reload` + curl verification).
- **LLM: Xiaomi MiMo `mimo-v2.6-pro`** via the OpenAI-compatible endpoint
  (`VAP_LLM_BASE_URL=https://token-plan-sgp.xiaomimimo.com/v1`,
  `VAP_LLM_MODEL=mimo-v2.6-pro`, `VAP_LLM_API_KEY` from server env — never
  committed). E2E-verified: a real chat completion must return through MiMo
  before the deployment is called done.
- `deploy/Dockerfile` — mcp + agent in one container (uvicorn), env-configured.
- `deploy/manifest.jps` — optional: installs the assistant as its own
  environment on the platform (dogfooding; its own env = existing system
  untouched), with the MCP endpoint exposed.
- `deploy/Caddyfile.snippet` — the path-route block for hermes.bannoe.it.
- Extension zip build script for distribution.

## 8. Repo layout

```
vap-copilot/
  docs/PLAN.md            # this file
  docs/API-CONTRACT.md    # extension <-> agent contract (frozen early)
  mcp/                    # MCP server + JelasticClient
  agent/                  # FastAPI chat + flows
  extension/              # Chrome MV3
  mock/                   # mock Jelastic API + mock dashboard page
  deploy/                 # Dockerfile, JPS, caddy snippet
  scripts/                # e2e_demo.sh, package_extension.sh
  tests/                  # pytest suite
  README.md
```

Python 3.11+, `mcp` SDK, `fastapi`, `httpx`, `uvicorn`, `pytest`.
No external paid services configured in-repo; LLM endpoint is env-configured.

## 9. Kanban task graph

| Card | Title | Depends on |
|---|---|---|
| T1 | Plan + repo scaffold | — |
| T2 | Mock Jelastic API + JelasticClient + unit tests | T1 |
| T3 | MCP server: tools, auth passthrough, safety gates | T2 |
| T4 | Agent backend: chat/SSE, guided flows, ask_user protocol | T3 |
| T5 | Browser extension MV3: FAB, side panel, settings | T3 |
| T6 | Deploy kit (Dockerfile, JPS, caddy) + README | T3 |
| T9 | One-command MCP onboarding CLI (npx install/serve) | T3 |
| T7 | Live e2e on VHC/demo + cleanup of mcp-test-* | T4, T5 |
| T8 | Docs + morning report | T7 |

## 10. Risks / open items

- **Live stand access**: `app.demo.jelastic.com` returns 403 (WAF) from this
  host and the cloud browser; `app.vhc.virtuozzo.com` rejects the provided
  password with `701 authentication failed` (SSO-only account or rotated
  password). Fallback: user logs in via browser -> session token passthrough
  (matches the product auth model anyway).
- **LLM key for the demo agent**: deployment-time decision (which provider/key
  pays). Nothing is wired to paid services in-repo; explicit approval needed.
- **Dashboard session-token capture** details verified only against the live
  dashboard; options-page paste is the fallback.
- Long Jelastic ops: client treats every mutation as fire-and-forget with
  observable-state polling (never trust client-side timeouts).
