# virtuozzo-am-mcp (npm package)

npx wrapper for the `virtuozzo-am-mcp` onboarding CLI (Python). It bundles
the `virtuozzo_am_mcp/` CLI and the `mcp/` server package so end users need
nothing but Node and Python:

```bash
npx virtuozzo-am-mcp login --host https://<your-panel-host>
npx virtuozzo-am-mcp install
```

## Requirements

- Node.js >= 18 (for npx)
- Python >= 3.11 on `PATH` (override with `VIRTUOZZO_AM_PYTHON`)
- the `httpx` Python package: `python3 -m pip install httpx`

## Build & publish

From the repository root:

```bash
bash packaging/npm/build.sh
cd packaging/npm/dist
npm publish
```

`build.sh` assembles `dist/` with `package.json`, `bin/virtuozzo-am-mcp.js`
(the shim) and `lib/{virtuozzo_am_mcp,mcp}/` (the Python code). The shim
sets `PYTHONPATH` to `lib/` and runs `python3 -m virtuozzo_am_mcp "$@"`.
