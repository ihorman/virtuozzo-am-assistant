# agent — Application Management Assistant agent backend

FastAPI chat backend for the Virtuozzo Application Manager assistant: SSE
chat, guided flows (deploy / add database / scale / troubleshoot), the
clarifying-question protocol, the mutation confirmation gate, error ->
diagnose -> auto-fix, and the in-process security review. The full wire
contract is frozen in `docs/API-CONTRACT.md` (v1.1); the UX bar is in
`docs/DESIGN.md`.

## Endpoints

| Route | Purpose |
|---|---|
| `POST /api/chat` | one turn as an SSE stream (see the contract for the event table) |
| `POST /api/answer` | resolve a paused `ask_user` / `confirm_action` / card action |
| `GET /api/context?conversation_id=` | echo of the last `X-Vap-Context` |
| `GET /api/audit?conversation_id=` | mutation audit trail ("What I changed") |
| `GET /healthz` | `{"status","llm_configured","version"}` — never echoes secrets |

Headers: `X-Jelastic-Host` + `X-Jelastic-Session` (platform session
passthrough — the agent carries only the end user's own rights), optional
`X-Vap-Context` (captured page context JSON).

## Answer shapes (`POST /api/answer`, additive v1.2)

Contract v1.1 defines the ask/confirm answers; the card-action answer is the
additive v1.2 shape this backend also accepts (one JSON body per pending
wait):

```json
{"conversation_id": "...", "ask_id": "ask-...", "answer": "text or option"}
{"conversation_id": "...", "confirm_id": "confirm-...", "approved": true}
{"conversation_id": "...", "action": "auto_fix|retry|open_logs|open_url|custom",
 "payload": {"repair_plan_id": "repair-..."}}
```

The third form resolves a clicked card action while the turn is paused on
`wait_card_action` (error cards with a one-click `auto_fix`, security cards
with one-click fixes). `payload.repair_plan_id` comes from the `card` event's
`actions[].payload`; repairs run guarded (recap -> Confirm -> apply ->
verify) and are refused if they could weaken security (see
`agent/diagnose.py: repair_plan_is_safe`).

## Architecture

- `config.py` — `VAP_LLM_*` env configuration (any OpenAI-compatible
  endpoint; no provider is hardcoded). `VAP_LLM_REASONING_EFFORT` is passed
  through only when it is one of `none|low|medium|high`.
- `llm.py` — minimal `/chat/completions` client; keys never leak into
  errors, logs or `/healthz`.
- `prompts.py` — system prompt + the text-based *directive protocol*: the
  model acts with one fenced `vap` JSON block per reply (`start_flow`,
  `ask_user`, `call_tool`, `finish`); narration outside the block streams as
  `token` events. Text-based on purpose: works with any OpenAI-compatible
  service regardless of function-calling support.
- `loop.py` — the turn loop; max 8 LLM rounds per turn; raw JSON never
  reaches the user.
- `flows.py` — deterministic guided flows; one question at a time; every
  mutation behind `confirm_action`; deploy ends with verify ("curl the env
  URL") and a security review card.
- `diagnose.py` — error -> diagnose -> auto-fix: evidence gathering, failure
  classification, template repair plans + guardrails.
- `tools.py` / `tools_util.py` — tool execution with masking, summaries and
  audit recording (no secret values anywhere).
- `conversation.py` — per-conversation history, pause protocol, audit trail,
  repair-plan registry. In-memory only; nothing is persisted.
- `events.py` — the SSE event model (single source of truth for the event
  table; `tests/test_flows.py` asserts streams against it).

## Testing

`tests/test_flows.py` drives every wizard end-to-end with a scripted
`FakeLLM` against the in-process mock (no network) and asserts the SSE
event contract exactly: questions asked, confirmations required, verify
runs, mutations only after an approved confirmation, error cards carry a
one-click `auto_fix` when a safe repair exists.

## Serving

`agent.main:app` is the ASGI entry point (`uvicorn agent.main:app`);
`deploy/serve.py` mounts it alongside the MCP server in one process.
