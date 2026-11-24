# Application Management Assistant — Extension <-> Agent API Contract (frozen v1.1)

Version 1.1 — adds `progress` and `card` stream events (design-driven rich
cards; see docs/DESIGN.md). All messages are JSON. The extension never talks to
the platform directly except for session-token capture; every platform call
goes through the agent (which passes the token through to the MCP layer).

## Common request headers (extension -> agent)

| Header | Value |
|---|---|
| `X-Jelastic-Host` | Platform base URL, e.g. `https://app.example.com` |
| `X-Jelastic-Session` | The user's Jelastic session token (from the logged-in dashboard) |
| `X-Vap-Context` | Optional JSON: `{"envName": "...", "page": "dashboard|env|marketplace", "url": "..."}` — captured page context |

External MCP clients use the same two `X-Jelastic-*` headers on `/mcp`
(or `JELASTIC_HOST` / `JELASTIC_SESSION` env vars in stdio mode).

## REST endpoints (agent)

### `POST /api/chat`

Request: `{"message": "...", "conversation_id": "uuid-or-null"}`

Response: `text/event-stream`, each event `data: {json}`:

| Event | Payload | Meaning |
|---|---|---|
| `token` | `{"text": "..."}` | assistant text chunk (streamed) |
| `tool_call` | `{"name": "...", "args": {...}, "call_id": "..."}` | tool about to run / running |
| `tool_result` | `{"call_id": "...", "ok": true, "summary": "..."}` | tool finished (summary only, no secrets) |
| `ask_user` | `{"question": "...", "options": ["..."], "allow_free": true, "ask_id": "..."}` | clarifying question; UI renders option chips + free text; answer sent as `POST /api/answer` |
| `confirm_action` | `{"summary": "...", "actions": ["..."], "confirm_id": "...", "danger": false}` | mutation gate; UI renders Confirm/Cancel; answer sent as `POST /api/answer` |
| `progress` | `{"label": "...", "call_id": "..."}` | long operation running; UI renders progress card with elapsed timer |
| `card` | `{"kind": "success\|info\|error\|security", "title": "...", "body_md": "...", "actions": [{"label": "...", "url": "...", "action": "auto_fix\|retry\|open_logs\|open_url\|custom", "payload": {...}}]}` | rich result card. Error cards MUST run diagnosis first and offer `action: "auto_fix"` as the primary button when a repair is possible; `payload` carries the repair plan id from `plan_repair`. `security` cards carry security-review findings with one-click fixes |
| `done` | `{"conversation_id": "..."}` | turn complete |
| `error` | `{"code": "...", "message": "..."}` | recoverable or fatal error |

### `POST /api/answer`

Resolves the pending `ask_user` / `confirm_action`:
`{"conversation_id": "...", "ask_id": "...", "answer": "text or option"}`
or `{"conversation_id": "...", "confirm_id": "...", "approved": true|false}`.

Rejected confirmations cancel the mutation and the agent narrates the abort.

### `GET /api/context`

Echoes the last `X-Vap-Context` for the conversation; used by the UI to show
"working with environment X".

### `GET /api/audit`

Compliance log for the conversation's platform actions: `[{"at": "...",
"tool": "...", "target": "...", "summary": "...", "ok": true}]` — mutation
audit trail (no secret values). The UI renders it as a "What I changed" card.

### `GET /healthz`

`{"status": "ok", "llm_configured": true|false, "version": "..."}` —
never echoes keys or tokens.

## Error codes

| Code | HTTP | Meaning |
|---|---|---|
| `session_expired` | 401 | platform session expired/invalid — re-open the dashboard |
| `no_credentials` | 401 | missing `X-Jelastic-Host`/`X-Jelastic-Session` |
| `platform_error` | 502 | platform returned an error (message carries result code) |
| `llm_not_configured` | 503 | agent has no LLM endpoint configured |
| `bad_request` | 400 | malformed request |

## SSE ordering guarantees

- `tool_call` always precedes its `tool_result`.
- `ask_user` / `confirm_action` always precede any `tool_call` they gate.
- `done` is the last event of a successful turn; `error` may end a turn instead.
- After `ask_user`/`confirm_action` the stream PAUSES (no further events until
  `/api/answer`); implementation detail: the stream stays open with keepalive
  comments (`: keepalive`) every 15s.

## Security notes

- Tokens/passwords never appear in `tool_result` summaries or logs.
- `env_delete` requires `confirm=true` in tool args AND an approved
  `confirm_action`; its password argument is forwarded once and never stored.
- All mutations require an approved `confirm_action` before the tool runs.
