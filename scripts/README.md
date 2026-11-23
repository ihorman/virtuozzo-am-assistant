# scripts

| File | Purpose |
|---|---|
| `package_extension.sh` | build `dist/virtuozzo-am-assistant-extension-v<version>.zip` from `extension/` (manifest.json at archive root; python3 zipfile — no zip binary needed) |
| `e2e_demo.sh` | (planned, T7) live end-to-end demo against the stand: throwaway `mcp-test-<ts>` environment, exercised then listed and deleted |

Usage: `scripts/package_extension.sh [extension-src-dir] [output-dir]`.
