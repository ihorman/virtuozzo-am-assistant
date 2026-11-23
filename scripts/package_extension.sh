#!/usr/bin/env bash
# Package the Chrome MV3 extension for distribution.
#
# Usage: scripts/package_extension.sh [extension-src-dir] [output-dir]
#   extension-src-dir  default: <repo>/extension
#   output-dir         default: <repo>/dist
#
# Produces <output-dir>/virtuozzo-am-assistant-extension-v<version>.zip with
# manifest.json at the ARCHIVE ROOT (Chrome requirement for loading/packing).
# Junk files (__pycache__, *.pyc, .DS_Store, *.zip) are excluded. Archiving
# is done with python3's zipfile module - no zip/unzip binaries required.
set -euo pipefail

die() { echo "package_extension: error: $*" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${1:-$ROOT/extension}"
OUT="${2:-$ROOT/dist}"

[ -d "$SRC" ] || die "extension dir not found: $SRC"
[ -f "$SRC/manifest.json" ] || die "manifest.json not found in $SRC (extension not built yet?)"
command -v python3 >/dev/null 2>&1 || die "'python3' is not installed"

# Validate the manifest and read the version for the artifact name.
VERSION="$(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
for key in ("manifest_version", "name", "version"):
    if key not in m:
        sys.exit(f"manifest.json is missing required key: {key}")
print(m["version"])
' "$SRC/manifest.json")" || die "manifest.json is not a valid MV3 manifest"

mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
SRC="$(cd "$SRC" && pwd)"
NAME="virtuozzo-am-assistant-extension-v${VERSION}.zip"
TARGET="$OUT/$NAME"

rm -f "$TARGET"
python3 - "$SRC" "$TARGET" <<'PYEOF' || die "archiving failed: $TARGET"
import os, sys, zipfile

src, target = sys.argv[1], sys.argv[2]
skip_dirs = {"__pycache__", ".git"}
skip_ext = {".pyc", ".pyo"}
skip_names = {".DS_Store"}

with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
    count = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in skip_dirs)
        for name in sorted(files):
            if name in skip_names or name.endswith(".zip"):
                continue
            if os.path.splitext(name)[1] in skip_ext:
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, src)
            z.write(full, rel)
            count += 1
if count == 0:
    sys.exit("no files found to package")
print(f"{count} files archived")
PYEOF

# Verify: manifest.json must sit at the archive root and the zip must be sound.
python3 -c '
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
names = z.namelist()
assert "manifest.json" in names, "manifest.json missing from archive root"
bad = z.testzip()
assert bad is None, f"corrupt member: {bad}"
print(f"verified: manifest.json at root, {len(names)} members, archive OK")
' "$TARGET" || die "archive verification failed: $TARGET"

SIZE="$(wc -c <"$TARGET" | tr -d ' ')"
echo "packaged: $TARGET ($SIZE bytes)"
echo "install: chrome://extensions -> Developer mode -> Load unpacked (extracted) or pack from the zip"
