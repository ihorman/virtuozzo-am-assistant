# mcp — MCP server + platform client

- `jelastic_client.py` (T2): async httpx client for the Jelastic/VAP REST
  subset. Session passthrough on every call, typed result-code mapping
  (`RESULT_CODE_MAP`), fire-and-forget mutations with observable-state
  polling (`wait_env_status` / `wait_env_gone` / `wait_nodes_ready` —
  a deadline raises `OperationTimeout` with the observed state, never
  concludes failure). Pass `transport=` to run against the in-process mock
  (tests) or use `from_headers()` for the extension contract headers.
- `protocol.py` (T3): JSON-RPC 2.0 + MCP wire helpers (versions
  2024-11-05 / 2025-03-26 / 2025-06-18, error codes, message validation).
  Implemented natively because this repo's top-level `mcp/` package shadows
  the PyPI `mcp` SDK on `sys.path` (imports of `mcp.server` would resolve
  here and fail); the required surface is small and spec-conformant.
- `tools.py` (T3): the v1 tool surface from docs/PLAN.md section 2
  (`envs_list` .. `op_result`, 21 tools), safety gates and execution:
  - mutating tools require `confirm: true` (strict JSON boolean) before any
    platform call; anything else is refused with `confirm_required`;
  - `env_delete` additionally requires the account `password`, forwarded
    once to `deleteenv`, never stored or echoed;
  - auth passthrough: `AuthConfig.from_headers()` (HTTP) /
    `from_env()` (stdio); missing credentials is an in-band
    `no_credentials` tool error;
  - tool failures return `isError: true` results (MCP in-band errors);
    a polling deadline returns `status: "in_progress"` with the observed
    state — never a failure.
- `server.py` (T3): `MCPServer` protocol core (`initialize`, `ping`,
  `tools/list`, `tools/call`, notifications, JSON-RPC batches) with two
  transports:
  - **Streamable HTTP** — `create_app()` serves `POST /mcp` (JSON responses,
    `Mcp-Session-Id` echoed, stateless) plus `GET /healthz`; GET/DELETE
    `/mcp` return 405 (no server-initiated stream / session termination).
    Per-request auth via `X-Jelastic-Host` / `X-Jelastic-Session`.
  - **stdio** — `run_stdio()`: newline-delimited JSON-RPC; auth via
    `JELASTIC_HOST` / `JELASTIC_SESSION`.
- `__main__.py` (T3): `python -m mcp --stdio` (default) or
  `python -m mcp --http --host 127.0.0.1 --port 8091`.

Tests: `tests/test_client.py`, `tests/test_mcp_tools.py` (against `mock/`,
no real network — enforced by a socket guard in `tests/conftest.py`).

## Security hardening (T10, docs/SECURITY.md)

`mcp/security.py` implements the confinement layer, enforced in
`execute_tool` before any platform call and covered by
`tests/test_security.py`:

- **Host allowlist + token pinning** — `X-Jelastic-Host`/`JELASTIC_HOST` must
  match `VAM_ALLOWED_HOSTS` (exact origins or `*.suffix` wildcards; empty =
  open mode). Redirects are never followed (`follow_redirects=False`; a 3xx
  is a `redirect_refused` error), so the session token cannot leave the
  pinned origin.
- **SSRF guard** — `deploy_archive` URLs and `deploy_jps` manifest URLs must
  be public http(s): userinfo and non-http(s) schemes are refused, every
  resolved address must be globally routable (private/loopback/link-local/
  metadata/CGNAT/IPv4-mapped blocked; decimal/hex/octal IP smuggling
  caught), and `SsrfGuard.safe_fetch` re-validates every redirect hop.
- **Strict validation** — anchored patterns on `env_name`/`nodeGroup`/
  `context`/stacks/domains, int/enum/length caps on every parameter, unknown
  parameters rejected (schema-driven via `security.validate_args`).
- **Secret masking** — `mask_secrets` scrubs secret-looking keys and inline
  `session=`/`token=` fragments from every tool result and audit entry
  (`GET /api/audit`, bounded); no credentials are stored server-side.
- **Rate/depth limits** — per-session tool calls/min and mutations/hour
  (`VAM_MCP_CALLS_PER_MIN`, `VAM_MCP_MUTATIONS_PER_HOUR`); `op_result`
  polling is capped at `VAM_MCP_MAX_POLL_SECONDS` (default 600). Trips
  return a plain `rate_limited` refusal.
- **Read-only mode** — `VAM_MCP_READONLY=1` refuses every mutating tool
  (`read_only_mode`) even with `confirm=true`.
- **Untrusted-data framing** — platform-returned text (logs, JPS messages)
  and extension `X-Vap-Context` are sanitized and wrapped in
  `<untrusted-data>` markers, so prompt-injection payloads stay DATA.
