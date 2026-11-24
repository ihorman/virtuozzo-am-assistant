# virtuozzo-am-assistant (VAP Copilot)

End-user AI assistant for the Virtuozzo Application Manager (V/AM, formerly
Jelastic / Virtuozzo Application Platform). It manages your cloud the way you
already talk — deploy an app, add a database, scale a node — always in your own
platform permissions, with an explicit confirmation before any change. Three
surfaces in one repo:

1. **MCP server** (`mcp/`) — the platform REST API as 21 confirm-gated MCP
   tools. Connect Claude Desktop, Cursor, Hermes or any MCP client ("bring
   your own agent"), or use it in-process from the agent backend.
2. **Agent backend** (`agent/`) — FastAPI chat + guided flows (deploy, add DB,
   scale, troubleshoot) with clarifying questions and action confirmations.
3. **Browser extension** (`extension/`, Chrome MV3) — a floating assistant
   button + side-panel chat on the platform dashboard. Zero platform changes.

User guide: [docs/USAGE.md](docs/USAGE.md) ·
Plan: [docs/PLAN.md](docs/PLAN.md) ·
Extension ↔ agent contract: [docs/API-CONTRACT.md](docs/API-CONTRACT.md) ·
Threat model: [docs/SECURITY.md](docs/SECURITY.md) ·
Status report: [docs/MORNING-REPORT-2026-11-24.md](docs/MORNING-REPORT-2026-11-24.md)

## Architecture

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
                                              +------------------------+
```

The same `mcp/` surface is also exposed standalone (Streamable HTTP at
`/mcp`, plus stdio mode) so external MCP clients connect with their own
session token.

### Auth model

| Consumer | Credential flow |
|---|---|
| Extension → agent | `X-Jelastic-Host` + `X-Jelastic-Session` headers (token captured from the logged-in dashboard) |
| External MCP client | the same two headers on `POST /mcp`, or `JELASTIC_HOST`/`JELASTIC_SESSION` env for stdio |
| Agent/MCP → platform | the caller's token on every REST call, stateless, never stored |

The platform enforces user rights natively — the assistant never stores
passwords (the one exception, the `env_delete` password, is forwarded once
per call and never echoed or logged). Expired sessions surface as a clear
`session_expired` error: "re-open the dashboard".

### Safety model (end-user product, mandatory)

- Read tools are free; **every mutating tool requires `confirm: true`**
  (the strict JSON boolean) in its arguments — refused with `confirm_required`
  otherwise, before any platform call.
- `env_delete` additionally requires the account `password` **and** a typed
  `passphrase` equal to the environment name (the live API refuses deletion
  with result 724 otherwise). The password/passphrase are forwarded once per
  call and never echoed or logged.
- Jelastic operations are fire-and-forget: `op_result` polls observable
  state; a timeout returns `status: "in_progress"`, never a false failure.
- The tool surface is closed (no fetch/exec/file tools) — see
  [docs/SECURITY.md](docs/SECURITY.md) for the full threat model.

## Repository layout

| Dir | Purpose |
|---|---|
| `docs/` | plan, API contract, security, usage, design |
| `mcp/` | MCP server (JSON-RPC + Streamable HTTP + stdio) + JelasticClient |
| `virtuozzo_am_mcp/` | `virtuozzo-am-mcp` onboarding CLI (login / install / serve) |
| `packaging/` | npx wrapper package for the onboarding CLI |
| `agent/` | FastAPI chat backend, SSE, guided flows |
| `extension/` | Chrome MV3 widget (FAB + side panel chat) |
| `mock/` | mock Jelastic API + mock dashboard page for tests/dev |
| `deploy/` | Dockerfile, combined ASGI entry, JPS manifest, Caddy snippet, systemd unit |
| `scripts/` | e2e demo + packaging scripts |
| `tests/` | pytest suite (network-free — a socket guard enforces it) |

Python 3.11+, `fastapi`, `uvicorn`, `httpx` (the MCP wire protocol is
implemented natively in `mcp/` — the repo's top-level `mcp/` package would
shadow the PyPI `mcp` SDK on `sys.path`, so the SDK is deliberately not used;
see `mcp/README.md`). No paid services are wired in-repo; the LLM endpoint is
env-configured at deployment time.

## Quickstart

```bash
git clone <this-repo> vap-copilot && cd vap-copilot
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
```

### 1. Run the mock platform (offline dev/demo)

```bash
python -m mock --port 8090     # REST API; account: demo / demo
```

### 2. Run the MCP server

```bash
# Streamable HTTP (per-request auth headers):
python -m mcp --http --host 127.0.0.1 --port 8091

# or stdio (auth from env):
JELASTIC_HOST=http://127.0.0.1:8090 JELASTIC_SESSION=<token> python -m mcp --stdio
```

Smoke-test HTTP mode against the mock:

```bash
curl -s http://127.0.0.1:8091/mcp \
  -H 'content-type: application/json' \
  -H 'X-Jelastic-Host: http://127.0.0.1:8090' \
  -H 'X-Jelastic-Session: <token>' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# -> {"jsonrpc":"2.0","id":1,"result":{"tools":[...21 tools...]}}
```

The server is stateless (one call = one platform client); `Mcp-Session-Id`
is echoed for client compatibility. `GET`/`DELETE /mcp` return 405 (legal:
no server-initiated stream / no session termination).

### 3. Run the combined server (agent + MCP, one process)

```bash
python -m deploy.serve            # 127.0.0.1:8080
curl -fsS http://127.0.0.1:8080/healthz
```

Route map: `POST /mcp` (MCP), `/api/*` (agent chat/audit), `GET /healthz`.
The agent backend is served automatically once `agent/main.py` exists;
until then the non-`/mcp` routes answer with a small placeholder while the
MCP surface stays fully live.

### 4. Tests

```bash
pytest            # network-free by construction (tests/conftest.py socket guard)
```

## Connect your own agent (one command)

Plug your own AI agent (Claude Code, Cursor, Hermes, or any MCP client) into
the platform — no dashboard scraping, no glue code:

```bash
npx virtuozzo-am-mcp login --host https://<your-panel-host>
npx virtuozzo-am-mcp install
```

- `login` authenticates (Keycloak/SSO browser flow, or `--email` + password
  prompt; `--session <token>` if you already have one) and stores the
  platform session in `~/.virtuozzo-am/config.json` (mode 0600).
- `install` detects your agent (Claude Code / Cursor / Hermes / generic) and
  writes its MCP config in ONE command — then prints the equivalent
  one-liner for manual paste.
- `serve` is the local stdio MCP bridge the agent spawns (same 21-tool
  surface as `mcp/`, session read from `~/.virtuozzo-am/config.json`).

No Node? Run the same CLI from a checkout: `python3 -m virtuozzo_am_mcp ...`
(Python 3.11+, `pip install httpx`). The npx package lives in
`packaging/npm/` (`bash packaging/npm/build.sh` assembles it).

Copy-paste variants:

```bash
# Zero-install, hosted MCP endpoint — one line, no local process:
claude mcp add --transport http virtuozzo-am https://<assistant-host>/mcp \
  --header "X-Jelastic-Host: https://<your-panel-host>" \
  --header "X-Jelastic-Session: <token from `virtuozzo-am-mcp login`>"

# Local stdio bridge (no hosted server needed):
claude mcp add virtuozzo-am -- npx -y virtuozzo-am-mcp serve --host https://<your-panel-host>

# Hermes:
hermes mcp add virtuozzo-am --command npx --args -y virtuozzo-am-mcp serve
# remote variant: `hermes mcp add virtuozzo-am --url <endpoint> --auth header`
# — the X-Jelastic-* headers go in ~/.hermes/config.yaml (`install` writes them)

# Any other MCP client: `virtuozzo-am-mcp install --agent generic` writes a
# plain {"mcpServers": {...}} JSON you can paste anywhere.
```

Per-client config shapes and the hosted endpoint details are below.

## Connect an external MCP client

Every client needs two things: the MCP endpoint and your platform session
(`X-Jelastic-Host` = your panel URL, `X-Jelastic-Session` = the session token
from the logged-in dashboard). Hosted endpoint: `https://<assistant-host>/mcp`
(e.g. `https://hermes.bannoe.it/am-assistant/mcp`). The token is yours —
the server only ever sends it to the `X-Jelastic-Host` origin.

Read tools work immediately. Mutating tools send `confirm: true` **only after
you explicitly confirmed the exact action**; `env_delete` also needs the
account `password` argument (forwarded once, never stored). Any client that
auto-ignores these gates is unsafe to connect.

### Claude Desktop

`claude_desktop_config.json` — hosted mode via the `mcp-remote` bridge:

```json
{
  "mcpServers": {
    "virtuozzo-am": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote", "https://<assistant-host>/mcp",
        "--header", "X-Jelastic-Host: https://<panel-host>",
        "--header", "X-Jelastic-Session: <session-token>"
      ]
    }
  }
}
```

or local stdio mode from a repo checkout (the `PYTHONPATH` entry points at
the repo root so `python3 -m mcp` resolves this repo's package):

```json
{
  "mcpServers": {
    "virtuozzo-am": {
      "command": "python3",
      "args": ["-m", "mcp", "--stdio"],
      "env": {
        "PYTHONPATH": "/path/to/vap-copilot",
        "JELASTIC_HOST": "https://<panel-host>",
        "JELASTIC_SESSION": "<session-token>"
      }
    }
  }
}
```

### Cursor

`.cursor/mcp.json` — native remote HTTP transport:

```json
{
  "mcpServers": {
    "virtuozzo-am": {
      "url": "https://<assistant-host>/mcp",
      "headers": {
        "X-Jelastic-Host": "https://<panel-host>",
        "X-Jelastic-Session": "<session-token>"
      }
    }
  }
}
```

### Hermes

`~/.hermes/config.yaml` — HTTP transport:

```yaml
mcp_servers:
  virtuozzo-am:
    url: "https://<assistant-host>/mcp"
    headers:
      X-Jelastic-Host: "https://<panel-host>"
      X-Jelastic-Session: "<session-token>"
```

or the stdio form (`command: python3`, `args: [-m, mcp, --stdio]`, `env:`
with `PYTHONPATH` + `JELASTIC_*` as in the Claude Desktop example). After a
restart the 21 tools appear as `mcp_virtuozzo_am_<tool>` alongside the
built-in tools.

## Deploy

### Docker (single image: MCP + agent, uvicorn)

```bash
docker build -t vap-copilot:0.1.0 -f deploy/Dockerfile .
docker run --rm -p 8080:8080 \
  -e VAP_LLM_BASE_URL=https://token-plan-sgp.xiaomimimo.com/v1 \
  -e VAP_LLM_MODEL=mimo-v2.6-pro \
  -e VAP_LLM_API_KEY="$VAP_LLM_API_KEY" \
  vap-copilot:0.1.0
curl -fsS http://127.0.0.1:8080/healthz
```

The image runs as a non-root user, carries a `/healthz` healthcheck, and
never bakes the LLM key in — it is passed at run time only.

### hermes.bannoe.it (systemd + existing Caddy)

Preferred host deployment (no Docker): systemd unit
[`deploy/vap-assistant.service`](deploy/vap-assistant.service) +
[`deploy/Caddyfile.snippet`](deploy/Caddyfile.snippet) routing
`/am-assistant/*` to `127.0.0.1:8080`. The Caddyfile on that host carries
many reverse proxies: full review, timestamped backup, `caddy validate`,
`caddy reload`, `curl` verification — the exact procedure is in the
snippet's header comment. Secrets live in `/etc/vap-copilot.env`
(see [`deploy/env.example`](deploy/env.example)); never committed.

### JPS manifest (optional, dogfooding)

[`deploy/manifest.jps`](deploy/manifest.jps) installs the assistant as **its
own environment** on a stand (one Docker node, existing system untouched).
Build/push the image to a registry first, then install the manifest and pass
the image URL as a setting. See the manifest header for details.

### Package the browser extension

```bash
scripts/package_extension.sh        # -> dist/virtuozzo-am-assistant-extension-v<version>.zip
```

Loads into `chrome://extensions` (Developer mode → Load unpacked / pack).

## Configuration reference

| Variable | Default | Used by | Purpose |
|---|---|---|---|
| `VAP_LLM_BASE_URL` | — | agent | OpenAI-compatible LLM endpoint (e.g. `https://token-plan-sgp.xiaomimimo.com/v1`) |
| `VAP_LLM_MODEL` | — | agent | model name (e.g. `mimo-v2.6-pro`) |
| `VAP_LLM_API_KEY` | — | agent | API key — server-side env only, never committed, logged or echoed |
| `VAP_SERVE_HOST` | `127.0.0.1` | `deploy/serve.py` | bind address (`0.0.0.0` inside the container) |
| `VAP_SERVE_PORT` | `8080` | `deploy/serve.py` | bind port |
| `JELASTIC_HOST` | — | `mcp` stdio | platform base URL |
| `JELASTIC_SESSION` | — | `mcp` stdio | the user's platform session token |

HTTP mode reads credentials per request from `X-Jelastic-Host` /
`X-Jelastic-Session` instead of the `JELASTIC_*` env vars (no cross-fallback).

## Status & verification (2026-11-24)

The full report with the verified/unverified split is
[docs/MORNING-REPORT-2026-11-24.md](docs/MORNING-REPORT-2026-11-24.md). Summary:

**Verified**
- Unit/integration suite: 237 tests, network-free, green (re-run 2026-11-24).
- Extension: headless-Chromium smoke against the mock agent + mock dashboard
  (FAB renders, full chat roundtrip incl. confirm gate and success card);
  19-state DESIGN.md screenshot set reviewed.
- Live e2e on a real stand (`app.vhc.virtuozzo.com`, VHC 8.15.1):
  `scripts/e2e_demo.sh` — **12 ok / 0 failed** (create env → deploy sample app
  → HTTP 200 → add DB node → vertical scale → read logs → live agent-chat turn
  over SSE → explicit deletion listing → delete → confirmed gone).
- Live REST shapes captured in
  [docs/live-api-notes.md](docs/live-api-notes.md) ("Live e2e-verified
  shapes").

**Not yet verified / open**
- MCP tool layer against live VHC: known method-name/shape mismatches
  (`getenvstat`, `getenvvariables`, deploy `url`, `deleteenv` passphrase) are
  being fixed in a follow-up pass; the mock + unit tests cover the contract,
  live coverage so far is the e2e harness and the agent chat.
- Stand auth for the demo stands: works with a browser session token
  (expires); SSO/password login flow not yet exercised end-to-end against the
  stand.
- LLM key for the demo agent deployment (env `VAP_LLM_API_KEY`) — to be
  provisioned by the operator at deploy time.

## Testing policy

- `pytest` is fully network-free (in-process mock + socket guard); 237 tests.
- **Live tests never touch existing systems**: they use throwaway
  `mcp-test-*` environments that are explicitly listed and deleted
  afterwards (`scripts/e2e_demo.sh`; deletion is confined to the
  `^mcp-test-` prefix and needs `passphrase=<envName>`).
- LLM endpoints are env-configured; nothing paid is called without a
  deployment-time key and explicit operator intent.
