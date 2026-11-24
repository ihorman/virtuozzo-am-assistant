# mock — mock Jelastic REST API

In-process test double for the Jelastic / VAP platform, faithful to the real
request conventions and result codes of the used API subset (see
`~/workspace/files/jelastic-complete-guide.md` and docs/PLAN.md §5).

## Layout

| File | Purpose |
|---|---|
| `codes.py` | result codes (0/1/9/501/701/2212/2314/4109/4132/11001) and env status codes |
| `state.py` | `MockJelastic` state machine: accounts, sessions, environments, nodes |
| `server.py` | `create_app(state)` — FastAPI app exposing the REST subset |
| `__main__.py` | `python -m mock [--port 8090]` — standalone server for dev/demo |

## Endpoints (`/1.0/...`)

- `users/authentication/rest/signin` (alias `authentication/signin`), `signout`
- `environment/control/rest/getenvs`, `getenvinfo`, `getenvstat`
- `environment/control/rest/createenvironment` (alias `createenv`)
- `environment/control/rest/startenv`, `stopenv`, `sleepenv`, `deleteenv`
- `environment/control/rest/changetopology`, `setcloudletscountbyid`
- `environment/deployment/rest/deployarchive` (alias `control/rest/deployapp`)
- `marketplace/jps/rest/install`
- `environment/control/rest/getlogs`
- `environment/control/rest/getenvvariables` / `setenvvariables`
  (+ `getcontainerenvvars` / `setcontainerenvvars` / `addcontainerenvvars` /
  `removecontainerenvvars`)

Responses use the real envelope: `{"result": 0, ...}` on success,
`{"result": <code>, "error": "..."}` on failure (always HTTP 200).

## Semantics

- **Auth**: wrong login/password -> `701`; missing/unknown/expired session ->
  `501` (older real stands use `9` for the same case). Sessions expire after
  `MockConfig.session_ttl` (default 1800s).
- **Async operations**: `createenvironment`, `startenv`, `stopenv`, `sleepenv`,
  `deleteenv` return immediately and transition status over
  `MockConfig.transition_seconds`. Transitions are lazy (applied on the next
  state read), so tests are deterministic without background timers.
- **`deleteenv`**: requires the account `password` (missing -> `invalid
  parameter [password] for method [DeleteEnv]`, wrong -> `701`). A retry while
  deletion runs -> `2212`; any operation against a deleting env -> `2212`.
- **Statuses**: `status` int (0 stopped / 1 running / 2 sleep — real catalog)
  plus `statusName` covering transient states (`creating`, `starting`,
  `stopping`, `sleeping`, `deleting`).
- **JPS install**: `jps` accepts a URL or inline manifest; non-ASCII or tab
  indentation in an inline manifest -> `11001` (real pitfall); invalid
  `settings` JSON -> `invalid parameter [settings]`.
- **Env names**: `^[a-z][a-z0-9-]{0,29}$`; a name collision -> `2314`
  (domain not available).

Mock-defined result codes (no verified real-stand equivalent): `2024`
environment not found, `2025` node not found, `2026` invalid parameter.

## Mock dashboard page

`dashboard.html` is a static, offline Jelastic-style dashboard for extension
development and smoke tests (marker `<meta name="vap-dashboard">`). Serve it
with `python3 -m http.server 8788 --directory mock` and open
`http://127.0.0.1:8788/dashboard.html?env=my-demo-env` — the Virtuozzo Application Management Assistant FAB
appears and captures the env context. See `extension/README.md`.

## Test support

`MockJelastic.expire_sessions()` forces session expiry;
`MockConfig(transition_seconds=..., session_ttl=...)` tunes timings.
Tests run against the app via `httpx.ASGITransport` or `fastapi.testclient`
(see `tests/test_mock.py`) — no real network, enforced by a socket guard in
`tests/conftest.py`.
