# mcp — MCP server + platform client

- `jelastic_client.py` (T2): async httpx client for the Jelastic/VAP REST
  subset. Session passthrough on every call, typed result-code mapping
  (`RESULT_CODE_MAP`), fire-and-forget mutations with observable-state
  polling (`wait_env_status` / `wait_env_gone` / `wait_nodes_ready` —
  a deadline raises `OperationTimeout` with the observed state, never
  concludes failure). Pass `transport=` to run against the in-process mock
  (tests) or use `from_headers()` for the extension contract headers.
- MCP server (tools, auth passthrough, safety gates) lands in T3.

Tests: `tests/test_client.py` (against `mock/`, no real network).
