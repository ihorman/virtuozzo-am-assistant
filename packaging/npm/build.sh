#!/usr/bin/env bash
# Assemble the publishable npx package in packaging/npm/dist/.
#
#   bash packaging/npm/build.sh
#   cd packaging/npm/dist && npm publish   # (release step, not part of T9)
#
# dist/ contains package.json + bin/ + lib/{virtuozzo_am_mcp,mcp}/ so the
# shim can run `python3 -m virtuozzo_am_mcp` with lib/ on PYTHONPATH.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DIST="$HERE/dist"

rm -rf "$DIST"
mkdir -p "$DIST/bin" "$DIST/lib"
cp "$HERE/package.json" "$HERE/README.md" "$DIST/"
cp "$HERE/bin/virtuozzo-am-mcp.js" "$DIST/bin/"
cp -R "$REPO/virtuozzo_am_mcp" "$DIST/lib/"
cp -R "$REPO/mcp" "$DIST/lib/"
find "$DIST" -name '__pycache__' -type d -exec rm -rf {} +

echo "assembled: $DIST"
