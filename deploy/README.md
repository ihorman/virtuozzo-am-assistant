# deploy — deployment kit

| File | Purpose |
|---|---|
| `Dockerfile` | one image: MCP + agent behind uvicorn (`python -m deploy.serve`), non-root, `/healthz` healthcheck |
| `serve.py` | combined ASGI entry: `/mcp` -> MCP server, everything else -> agent backend (`agent.main:app`, placeholder until it lands) |
| `vap-assistant.service` | systemd unit for hermes.bannoe.it (systemd preferred over Docker there) |
| `Caddyfile.snippet` | minimal `handle_path /am-assistant/*` block + the safe edit/reload/verify procedure for the shared Caddyfile |
| `manifest.jps` | optional JPS package: installs the assistant as its own environment on a stand (own env, existing system untouched) |
| `env.example` | `/etc/vap-copilot.env` template (LLM endpoint + bind) — real file never committed |

Root README's **Deploy** section covers the commands; the Caddy snippet
header carries the exact procedure for hermes.bannoe.it.
