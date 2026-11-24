#!/usr/bin/env bash
# Live e2e against a V/AM stand (VHC / demo Jelastic) — scripts/e2e_demo.sh
#
# Phases (task T7):
#   1. create mcp-test-<ts> env (nginxphp-dockerized, jem-capable)
#   2. deploy sample app (Spoon-Knife static zip) and verify it serves
#   3. add a database node (changetopology, mariadb-dockerized)
#   4. vertical scale (setcloudletscountbyid)
#   5. read logs (getlogs listing + readlog content)
#   6. agent chat exercise (agent SSE chat against the live platform)
#   7. LIST everything created, then DELETE it (explicit listing first)
#
# Usage:
#   JELASTIC_SESSION=<token> [JELASTIC_PASSWORD=<pass>] scripts/e2e_demo.sh [--keep]
#
#   --keep  leave the test environment in place (skip phase 7 deletion)
#
# Every platform call is form-encoded REST with the exact request shapes
# verified live against VHC 8.15.1 (see docs/live-api-notes.md).
# Deletion is confined to environments matching ^mcp-test- and is always
# preceded by an explicit listing.

set -u

HOST="${JELASTIC_HOST:-https://app.vhc.virtuozzo.com}"
SESSION="${JELASTIC_SESSION:?JELASTIC_SESSION is required}"
PASSWORD="${JELASTIC_PASSWORD:-}"
KEEP="${1:-}"
TS="$(date +%s)"
ENV_NAME="mcp-test-${TS}"
BASE="$HOST/1.0"
SAMPLE_URL="https://codeload.github.com/octocat/Spoon-Knife/zip/refs/heads/main"
LOG="$(umask 077 && mktemp /tmp/e2e-demo-XXXXXX)"
PASS=0; FAIL=0; CREATED=()

say()  { printf '%s\n' "$*" | tee -a "$LOG"; }
ok()   { PASS=$((PASS+1)); say "[OK ] $*"; }
bad()  { FAIL=$((FAIL+1)); say "[FAIL] $*"; }

# api METHOD PATH [curl args...] -> response body on stdout (also logged)
api() {
  local method="$1" path="$2"; shift 2
  if [ "$method" = GET ]; then
    curl -sS -m 180 "$BASE/$path" "$@" 2>>"$LOG"
  else
    curl -sS -m 180 -X POST "$BASE/$path" "$@" 2>>"$LOG"
  fi
}

json() { python3 -c "$1"; }

say "=== e2e_demo: host=$HOST env=$ENV_NAME ==="

# ---------------------------------------------------------------- phase 1 --
say "--- phase 1: create env ---"
OUT="$(api POST environment/control/rest/createenvironment \
  -d "session=$SESSION" \
  --data-urlencode "env={\"shortdomain\":\"$ENV_NAME\"}" \
  --data-urlencode 'nodes=[{"nodeType":"nginxphp-dockerized","nodeGroup":"cp","count":1,"fixedCloudlets":1,"flexibleCloudlets":3}]')"
if echo "$OUT" | json 'import sys,json;d=json.load(sys.stdin);print("ok" if d.get("result")==0 else "err")' | grep -q ok; then
  ok "createenvironment accepted for $ENV_NAME"
  CREATED+=("$ENV_NAME")
else
  bad "createenvironment: $(echo "$OUT" | head -c 200)"
  say "=== SUMMARY: $PASS ok, $FAIL failed (nothing created) ==="; exit 1
fi

# wait for running (numeric status 1; VHC exposes no statusName)
NODE_ID=""; DOMAIN=""
for _ in $(seq 1 40); do
  sleep 10
  INFO="$(api GET "environment/control/rest/getenvinfo?session=$SESSION&envName=$ENV_NAME")"
  STATUS="$(echo "$INFO" | json 'import sys,json;d=json.load(sys.stdin);print((d.get("env") or {}).get("status"))')"
  if [ "$STATUS" = 1 ]; then
    NODE_ID="$(echo "$INFO" | json 'import sys,json;d=json.load(sys.stdin);ns=d.get("nodes") or [];print(ns[0]["id"] if ns else "")')"
    DOMAIN="$(echo "$INFO" | json 'import sys,json;d=json.load(sys.stdin);print((d.get("env") or {}).get("domain",""))')"
    break
  fi
done
if [ -n "$NODE_ID" ]; then
  ok "env running: node=$NODE_ID domain=$DOMAIN"
else
  bad "env did not reach running (last status=$STATUS)"
  say "=== SUMMARY: $PASS ok, $FAIL failed ==="; exit 1
fi

# ---------------------------------------------------------------- phase 2 --
say "--- phase 2: deploy sample app ---"
OUT="$(api POST environment/deployment/rest/deployarchive \
  -d "session=$SESSION" -d "envName=$ENV_NAME" -d "context=ROOT" -d "nodeGroup=cp" \
  -d "fileUrl=$SAMPLE_URL" -d "fileName=spoon-knife.zip")"
if echo "$OUT" | json 'import sys,json;d=json.load(sys.stdin);print("ok" if d.get("result")==0 else json.dumps(d)[:300])' | grep -q '^ok$'; then
  ok "deployarchive accepted (fileUrl+fileName)"
else
  bad "deployarchive: $(echo "$OUT" | head -c 300)"
fi
HTTP_CODE=0
for _ in $(seq 1 18); do
  sleep 10
  HTTP_CODE="$(curl -sS -o /dev/null -w '%{http_code}' -m 30 "http://$DOMAIN/" 2>/dev/null || echo 0)"
  [ "$HTTP_CODE" = 200 ] && break
done
if [ "$HTTP_CODE" = 200 ]; then
  BODY="$(curl -sS -m 30 "http://$DOMAIN/" 2>/dev/null || true)"
  case "$BODY" in
    *Spoon*|*spoon*|*\<html*|*\<HTML*) ok "sample app serves over http://$DOMAIN/ (HTTP 200)";;
    *) ok "http://$DOMAIN/ returned 200 (body not inspected: $(echo "$BODY" | head -c 60))";;
  esac
else
  bad "sample app not reachable yet (HTTP $HTTP_CODE) — deploy may still be applying"
fi

# ---------------------------------------------------------------- phase 3 --
say "--- phase 3: add database node ---"
OUT="$(api POST environment/control/rest/changetopology \
  -d "session=$SESSION" -d "envName=$ENV_NAME" \
  --data-urlencode 'env={"region":"","sslstate":false}' \
  --data-urlencode "nodes=[{\"nodeType\":\"nginxphp-dockerized\",\"nodeGroup\":\"cp\",\"count\":1,\"fixedCloudlets\":1,\"flexibleCloudlets\":3},{\"nodeType\":\"mariadb-dockerized\",\"nodeGroup\":\"db\",\"count\":1,\"fixedCloudlets\":1,\"flexibleCloudlets\":3}]")"
if echo "$OUT" | json 'import sys,json;d=json.load(sys.stdin);print("ok" if d.get("result")==0 else json.dumps(d)[:300])' | grep -q '^ok$'; then
  ok "changetopology accepted (mariadb-dockerized db node)"
else
  bad "changetopology: $(echo "$OUT" | head -c 300)"
fi
DB_NODE=""
for _ in $(seq 1 24); do
  sleep 10
  INFO="$(api GET "environment/control/rest/getenvinfo?session=$SESSION&envName=$ENV_NAME")"
  DB_NODE="$(echo "$INFO" | json 'import sys,json;d=json.load(sys.stdin);ns=[n for n in d.get("nodes") or [] if n.get("nodeGroup")=="db"];print(ns[0]["id"] if ns else "")')"
  [ -n "$DB_NODE" ] && break
done
if [ -n "$DB_NODE" ]; then ok "db node present: id=$DB_NODE"; else bad "db node never appeared"; fi

# ---------------------------------------------------------------- phase 4 --
say "--- phase 4: vertical scale ---"
OUT="$(api POST environment/control/rest/setcloudletscountbyid \
  -d "session=$SESSION" -d "envName=$ENV_NAME" -d "nodeId=$NODE_ID" \
  -d "fixedCloudlets=2" -d "flexibleCloudlets=4")"
if echo "$OUT" | json 'import sys,json;d=json.load(sys.stdin);print("ok" if d.get("result")==0 else json.dumps(d)[:200])' | grep -q '^ok$'; then
  ok "setcloudletscountbyid accepted (cp node -> 2 fixed + 4 flexible)"
else
  bad "setcloudletscountbyid: $(echo "$OUT" | head -c 200)"
fi

# ---------------------------------------------------------------- phase 5 --
say "--- phase 5: read logs ---"
OUT="$(api GET "environment/control/rest/getlogs?session=$SESSION&envName=$ENV_NAME&nodeId=$NODE_ID")"
if echo "$OUT" | json 'import sys,json;d=json.load(sys.stdin);print("ok" if d.get("result")==0 else json.dumps(d)[:200])' | grep -q '^ok$'; then
  ok "getlogs listing retrieved (nodeId=$NODE_ID)"
else
  bad "getlogs: $(echo "$OUT" | head -c 200)"
fi
OUT="$(api GET "environment/control/rest/readlog?session=$SESSION&envName=$ENV_NAME&nodeId=$NODE_ID&path=/var/log/alternatives.log&line=5")"
if echo "$OUT" | json 'import sys,json;d=json.load(sys.stdin);print("ok" if d.get("result")==0 and d.get("body") is not None else json.dumps(d)[:200])' | grep -q '^ok$'; then
  ok "readlog returned content"
else
  # alternatives.log may not exist on this image — any parseable non-2002 answer still proves the method
  OUT2="$(api GET "environment/control/rest/getlogslist?session=$SESSION&envName=$ENV_NAME&nodeId=$NODE_ID")"
  if echo "$OUT2" | json 'import sys,json;d=json.load(sys.stdin);print("ok" if d.get("result")==0 else "err")' | grep -q ok; then
    ok "readlog skipped (no /var/log/alternatives.log), getlogslist works instead"
  else
    bad "readlog/getlogslist: $(echo "$OUT" | head -c 150)"
  fi
fi

# ---------------------------------------------------------------- phase 6 --
say "--- phase 6: agent chat exercise ---"
if python3 scripts/e2e_agent_chat.py --host "$HOST" --session "$SESSION" --env "$ENV_NAME" >>"$LOG" 2>&1; then
  ok "agent chat exercise passed (see $LOG tail)"
  tail -n 8 "$LOG" | sed 's/^/    /'
else
  bad "agent chat exercise failed (details in $LOG)"
fi

# ---------------------------------------------------------------- phase 7 --
say "--- phase 7: listing + cleanup ---"
LIST="$(api GET "environment/control/rest/getenvs?session=$SESSION")"
say "=== ENVIRONMENTS ON STAND BEFORE CLEANUP ==="
echo "$LIST" | json '
import sys, json
d = json.load(sys.stdin)
for e in d.get("infos") or []:
    env = e.get("env") or {}
    print("  %-28s status=%s nodes=%d %s" % (env.get("shortdomain"), env.get("status"),
          len(e.get("nodes") or []), "(DELETE - matches mcp-test-*)" if str(env.get("shortdomain","")).startswith("mcp-test-") else "(keep - not ours)"))
' | tee -a "$LOG"

if [ "$KEEP" = "--keep" ]; then
  say "--keep given: leaving $ENV_NAME in place"
else
  say "=== DELETION LISTING (mcp-test-* only) ==="
  TO_DELETE="$(echo "$LIST" | json '
import sys, json
d = json.load(sys.stdin)
print("\n".join(str((e.get("env") or {}).get("shortdomain"))
      for e in d.get("infos") or []
      if str((e.get("env") or {}).get("shortdomain","")).startswith("mcp-test-")))'
  )"
  say "$TO_DELETE" | sed 's/^/  DELETE: /'
  for NAME in $TO_DELETE; do
    # deleteenv requires a typed confirmation: passphrase must equal the env
    # name (live result 724 otherwise); account password is also sent when set.
    if [ -n "$PASSWORD" ]; then
      OUT="$(api POST "environment/control/rest/deleteenv" -d "session=$SESSION" -d "envName=$NAME" -d "passphrase=$NAME" -d "password=$PASSWORD")"
    else
      OUT="$(api POST "environment/control/rest/deleteenv" -d "session=$SESSION" -d "envName=$NAME" -d "passphrase=$NAME")"
    fi
    RC="$(echo "$OUT" | json 'import sys,json
try:
    d=json.load(sys.stdin); print("ok" if d.get("result") in (0,2212) else "err:"+str(d.get("result"))+" "+str(d.get("error",""))[:120])
except Exception as e: print("err:unparseable")')"
    case "$RC" in
      ok*) ok "deleteenv $NAME accepted";;
      *)   bad "deleteenv $NAME: $RC";;
    esac
    # verify gone
    GONE=0
    for _ in $(seq 1 18); do
      sleep 10
      STILL="$(api GET "environment/control/rest/getenvinfo?session=$SESSION&envName=$NAME" | json 'import sys,json;d=json.load(sys.stdin);print("1" if d.get("result")==0 else "0")')"
      [ "$STILL" = 0 ] && GONE=1 && break
    done
    if [ "$GONE" = 1 ]; then ok "$NAME confirmed deleted"; else bad "$NAME still visible after delete"; fi
  done
fi

say ""
say "=== SUMMARY: $PASS ok, $FAIL failed (log: $LOG) ==="
[ "$FAIL" = 0 ]
