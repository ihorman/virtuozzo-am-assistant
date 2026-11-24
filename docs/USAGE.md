# virtuozzo-am-assistant — User Guide

An AI assistant for the Virtuozzo Application Manager (V/AM, formerly
Jelastic / VAP). It manages your cloud the way you already talk: deploy an
app, add a database, scale a node — with guided questions, in your own
permissions, from three surfaces:

1. **Dashboard widget** — a browser extension that adds an AI assistant to the
   platform dashboard (side-panel chat + a floating button).
2. **Your own agent over MCP** — connect Claude Code, Cursor, Hermes or any
   MCP client to the platform with one command.
3. **Hosted assistant backend** — the brain both of the above talk to (you can
   self-host it in one environment on the platform itself).

---

## 1. Dashboard widget (browser extension)

### Install (developer mode, 1 minute)

1. Build or download the extension (`scripts/package_extension.sh` produces
   `dist/virtuozzo-am-assistant-extension-v<version>.zip`).
2. Open `chrome://extensions`, enable **Developer mode**.
3. **Load unpacked** -> pick the `extension/` directory (or unzip the release).
4. (Optional) Open the extension **Options** to change the assistant endpoint
   URL. The default is the hosted assistant
   (`https://hermes.bannoe.it/am-assistant`); for offline development point it
   at your local backend (e.g. `http://127.0.0.1:8787`).

### Use

Open the platform dashboard and sign in as usual. You get:

- a floating **AI Assistant** button on dashboard pages;
- a **side panel** chat (click the extension icon or the button);
- the assistant sees **which environment you are looking at** and asks before
  it acts.

Try things like:

> "deploy a Node.js app from this archive URL to a new environment"
> "add a PostgreSQL database to env-12345"
> "my app is slow, check it and scale if needed"
> "set up SSL for my environment"

The assistant guides you: it asks one clarifying question at a time
(stack? resources? domain?), shows a recap, asks for **confirmation**, executes,
and verifies the result (e.g. curls the deployed app) before reporting.

### Safety model

- The assistant only ever acts **with your own platform permissions** — it
  passes your dashboard session to the API; it never stores your password.
- Every change requires an explicit **Confirm** in the chat.
- Deleting an environment is double-confirmed and additionally asks you to
  type the account password once and a confirmation passphrase equal to the
  environment name (the live API requires both).

## 2. Deploy anything - any language

"Deploy my Rust app from https://github.com/me/my-app" just works - for ANY
language. The assistant reads your project's manifests (package.json,
Cargo.toml, mix.exs, go.mod, pom.xml, *.csproj, build.zig, CMakeLists.txt,
Makefile, ...), proposes the detected stack as its first chip ("Rust 1.80
(detected from Cargo.toml)"), and routes the deploy through a tiered table so
an app in every language can start:

| Tier | Languages | How it deploys |
|---|---|---|
| 1 - native stack | Node.js, Python, Java, PHP, Go, Ruby, .NET | straight onto the platform stack (git deploys stay connected to your repo) |
| 2 - runtime image | Rust, Elixir, Zig, C/C++, Deno, Bun, Haskell, OCaml, Crystal, Dart, Swift, Nim, static sites | a docker node from the official runtime image (rust:1.80-slim, node:20, ...) - your code is fetched, built and started INSIDE the container, no image build needed |
| 3 - your own image | any (Dockerfile / registry image) | a docker node from your image |
| 4 - VPS bootstrap | exotic custom runtimes | a VPS node with a generated bootstrap script (install toolchain, build, run as a service) |

Detected versions come from your manifests and toolchain files
(`.python-version`, `rust-toolchain.toml`, `engines` in package.json, ...),
and the app port follows framework conventions (express/fastify 3000,
flask/django 8000/8080, spring 8080, rails 3000, phoenix 4000, actix/axum
8080) - override it any time with the `PORT` env var. Your app must bind
`0.0.0.0` (the wizard sets `PORT` and documents this).

If the language cannot be inferred, the assistant asks **"Which runtime
image should I use?"** - answer with your own registry image, or pick the
VPS-node fallback. Every path reaches a deployable plan; nothing dead-ends.

After every deploy the assistant verifies the result (waits for the nodes,
curls the app URL, then checks the app actually started - status and page
title), and offers a one-click auto-fix when it did not. The last step is
always the security review card.

## 3. Connect your own agent via MCP (one command)

```bash
npx virtuozzo-am-mcp login --host https://<your-panel-host>
npx virtuozzo-am-mcp install
```

- `login` opens the platform SSO login (or takes a password), stores the
  platform session in `~/.virtuozzo-am/config.json` (file mode 0600).
- `install` detects your agent (Claude Code, Cursor, Hermes, or generic) and
  writes its MCP config — then prints the equivalent one-liner for manual use:

```bash
# Remote mode (hosted MCP endpoint) — one line, e.g. for Claude Code:
claude mcp add --transport http virtuozzo-am https://assistant.example.com/mcp \
  --header "X-Jelastic-Session: <token from `virtuozzo-am-mcp login`>"

# Local mode (stdio bridge, no hosted server needed):
claude mcp add virtuozzo-am -- npx virtuozzo-am-mcp serve --host https://<panel>
```

Same tool set in every mode: list/create/start/stop environments, deploy
apps and marketplace JPS, add databases, scale, read logs, manage variables
and SSL.

## 4. Hosted assistant backend (self-hosting)

Run the assistant itself on the platform (its own environment — the existing
system stays untouched):

```bash
# Docker
docker run -p 8787:8787 \
  -e VAP_LLM_BASE_URL=https://<openai-compatible-endpoint> \
  -e VAP_LLM_API_KEY=<key> -e VAP_LLM_MODEL=<model> \
  ghcr.io/ihorman/virtuozzo-am-assistant
```

or install `deploy/manifest.jps` from the dashboard (it provisions the
environment + the service). Configure the LLM endpoint with the standard
OpenAI-compatible env vars (`VAP_LLM_BASE_URL`, `VAP_LLM_API_KEY`,
`VAP_LLM_MODEL`) — no key is baked in.

Endpoints: `POST /api/chat` (SSE), `GET /api/context`, `GET /healthz`,
`MCP /mcp` (Streamable HTTP). See `docs/API-CONTRACT.md`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "session expired" in chat | Re-open the dashboard (re-login) — the extension picks up the new session |
| Extension button missing | Check the panel host matches the extension's host permission; reload the extension |
| `npx virtuozzo-am-mcp login` fails SSO | Complete the browser login in the opened window; corporate SSO may require VPN |
| Tool call refused "needs confirmation" | That is the safety gate — answer the confirm card in chat |
| Assistant can't reach platform | Verify `--host` / options page URL is the panel root (the one whose dashboard you open) |

More detail: `docs/PLAN.md` (architecture), `docs/API-CONTRACT.md` (event
contract), `docs/live-api-notes.md` (platform API behaviors).
