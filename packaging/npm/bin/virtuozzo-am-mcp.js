#!/usr/bin/env node
// npx shim for virtuozzo-am-mcp: runs the bundled Python CLI.
//
// The package bundles the `virtuozzo_am_mcp/` CLI and the `mcp/` server
// package under lib/; this shim puts lib/ on PYTHONPATH and execs
// `python3 -m virtuozzo_am_mcp`. Requires python3 (>=3.11) on PATH and the
// `httpx` package (`python3 -m pip install httpx`).
'use strict';

const { spawnSync } = require('child_process');
const path = require('path');

const root = path.join(__dirname, '..');
const lib = path.join(root, 'lib');
const python = process.env.VIRTUOZZO_AM_PYTHON || 'python3';

const env = { ...process.env };
env.PYTHONPATH = env.PYTHONPATH ? lib + path.delimiter + env.PYTHONPATH : lib;

const result = spawnSync(python, ['-m', 'virtuozzo_am_mcp', ...process.argv.slice(2)], {
  stdio: 'inherit',
  env,
});

if (result.error) {
  if (result.error.code === 'ENOENT') {
    console.error(
      'virtuozzo-am-mcp: python3 not found on PATH.\n' +
      'Install Python 3.11+ (or set VIRTUOZZO_AM_PYTHON to its path), and\n' +
      'make sure the httpx package is available: python3 -m pip install httpx');
  } else {
    console.error('virtuozzo-am-mcp: ' + result.error.message);
  }
  process.exit(1);
}
process.exit(result.status === null ? 1 : result.status);
