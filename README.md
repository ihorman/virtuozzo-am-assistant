# virtuozzo-am-assistant

End-user AI assistant for the Virtuozzo Application Manager (V/AM, formerly
Jelastic / Virtuozzo Application Platform):
MCP server + agent backend + Chrome MV3 extension. See
[docs/USAGE.md](docs/USAGE.md) for how to use everything (widget, one-command
MCP connect, self-hosting),
[docs/PLAN.md](docs/PLAN.md) for the implementation plan and
[docs/API-CONTRACT.md](docs/API-CONTRACT.md) for the frozen extension ↔ agent
contract.

## Layout

| Dir | Purpose |
|---|---|
| `docs/` | plan + API contract |
| `mcp/` | MCP server + JelasticClient (platform REST wrapper) |
| `agent/` | FastAPI chat backend, SSE, guided flows |
| `extension/` | Chrome MV3 widget (FAB + side panel chat) |
| `mock/` | mock Jelastic API + mock dashboard page for tests/dev |
| `deploy/` | Dockerfile, JPS manifest, Caddy snippet |
| `scripts/` | e2e demo + packaging scripts |
| `tests/` | pytest suite |

Python 3.11+, `mcp` SDK, `fastapi`, `httpx`, `uvicorn`, `pytest`.
No paid services are wired in-repo; the LLM endpoint is env-configured
(`VAP_LLM_BASE_URL` / `VAP_LLM_API_KEY` / `VAP_LLM_MODEL`).

Testing constraint: never touch existing platform systems — live tests use
throwaway `mcp-test-*` environments that are listed and deleted afterwards.
