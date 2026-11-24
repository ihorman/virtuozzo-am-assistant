# extension — Application Management Assistant browser extension (Chrome MV3)

The AI assistant for the Virtuozzo Application Manager dashboard: a floating
**AI Assistant** button on dashboard pages, a streaming chat side panel with
guided flows, and a settings escape hatch. Design system and UX bar: [`docs/DESIGN.md`](../docs/DESIGN.md).
Wire contract (frozen): [`docs/API-CONTRACT.md`](../docs/API-CONTRACT.md).

## Install (unpacked, for the demo)

1. Chrome/Chromium 116+ → `chrome://extensions`
2. Enable **Developer mode**
3. **Load unpacked** → select this `extension/` directory

That's it — the assistant works with zero configuration (hosted default agent
endpoint, session captured automatically). The toolbar action opens the side
panel; the FAB on dashboard pages opens it too.

## What's inside

| Path | Purpose |
|---|---|
| `manifest.json` | MV3 manifest (`sidePanel`, `storage`, `activeTab`, `cookies`; host permissions so any stand works) |
| `background.js` | service worker: settings, session capture, **fetch proxy** (all agent traffic + auth headers live here), side-panel behavior |
| `content/content.js` | FAB "AI Assistant" (open shadow DOM), env context capture (`X-Vap-Context`), best-effort session-token scan |
| `sidepanel/` | chat UI: SSE streaming, markdown, wizard step dots, ask_user chips, confirm cards (danger = typed confirmation), progress cards (elapsed timer), success/error/security/audit cards |
| `options/` | agent URL, platform host, session token paste fallback, connection diagnostics |
| `lib/api.js` | shared SSE parser, markdown renderer, error-code translations |
| `fonts/` | Inter + JetBrains Mono (bundled, offline-safe) |
| `icons/` | extension icons |

## Settings (optional)

- **Agent URL** — chat backend (default: hosted endpoint; can point anywhere,
  e.g. `python3 -m tests.mock_agent --port 8787` for offline development).
- **Platform host** — where the FAB appears and where the session is captured;
  empty = auto-detect (any page that looks like a Jelastic dashboard).
- **Session token** — paste fallback when automatic capture fails. Stored in
  `chrome.storage.local` only, never synced, never logged.

## Offline development

```bash
# 1. scripted mock agent (every contract event, deterministic scenarios)
python3 -m tests.mock_agent --port 8787

# 2. mock dashboard
python3 -m http.server 8788 --directory mock
# open http://127.0.0.1:8788/dashboard.html?env=my-demo-env
```

Then point the extension's **Agent URL** at `http://127.0.0.1:8787`. The FAB
appears on the mock dashboard; the chat roundtrip works fully (message →
streamed reply → ask chips → confirm → progress → success card).

Mock agent scenarios (by message text): `deploy…`, `…database…`,
`…security…`, `error`/`fail`, `slow`/`backup`, `bill`, `faster`, anything
else = friendly markdown reply.

## Smoke test (acceptance)

```bash
python3 -m pytest tests/test_extension_smoke.py -q
```

Headless Chromium + the real unpacked extension against the mock agent and the
mock dashboard: asserts the FAB renders and the sidepanel chat roundtrip works,
and captures the DESIGN.md screenshot set (all states, dark + light) into
`tests/screenshots/`. Container note: on hosts with a tight task cgroup cap the
test runs Chromium with `--single-process` (see `LEAN_ARGS` in the test module).

## Security notes

- The fetch proxy only ever talks to the configured agent origin — it is not
  an open proxy.
- Session tokens never appear in URLs, logs, or `tool_result` summaries; the
  token lives in `chrome.storage.local` (not synced).
- Content-script token scanning only reports to the background when the page
  matches the configured platform host.
