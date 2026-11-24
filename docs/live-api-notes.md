# Live platform API notes (verified 2026-11-23/24 against VHC 8.15.1)

Verified facts from live probing of a VHC/Jelastic-family panel
(`app.vhc.virtuozzo.com`), plus the full end-to-end run of
`scripts/e2e_demo.sh` on 2026-11-24 (12 ok / 0 failed). Account identifiers
are redacted. These shapes are what `mcp/jelastic_client.py` and the mock must
honor. The section "Live e2e-verified shapes" at the bottom is the
authoritative write-up of the e2e findings.

## Auth

- Dashboard login is Keycloak SSO (`auth.<host>/realms/platform`, client
  `platform-public-client`, PKCE). REST `users/authentication/rest/signin`
  with login+password returns `result: 701 authentication failed` for
  SSO-federated accounts — password grant is disabled server-side.
- The dashboard keeps a **platform session token** (32-40 char hex-ish string)
  after the SSO redirect. Every REST call authenticates with `session=<token>`
  (query or form param). This token is exactly what the browser extension
  captures and passes through as `X-Jelastic-Session`.
- A live token confirmed working: `getenvs` returns `result: 0`.
- `getaccountinfo` on this stand answers `result: 1702` (unknown/unsupported
  method) — do not rely on it; treat 17xx as "method not available" and fall
  back to `getenvs` + `getenvinfo` for identity-ish checks.

## getenvs

`GET /1.0/environment/control/rest/getenvs?session=<token>`

```json
{
  "result": 0,
  "infos": [
    {
      "env": { "appid": ..., "attributes": {...}, "contexts": [...],
               "createdOn": ..., "creatorUid": ..., "displayName": "...",
               "domain": "<name>.<region>.<platform-domain>", "envName": "...",
               "extdomains": [...], "hardwareNodeGroup": "...", "hostGroup": "...",
               "isFirewallEnabled": ..., "isRemote": ..., "isTransferring": ...,
               "ishaenabled": ..., "ownerUid": ..., "shortdomain": "...",
               "sslstate": ..., "status": 1, "uid": ... },
      "envGroups": [...], "nodeGroups": [...], "nodes": [...], "result": 0
    }
  ]
}
```

## getenvinfo (FLAT on VHC — differs from classic Jelastic!)

`GET /1.0/environment/control/rest/getenvinfo?session=<token>&envName=<name>`

VHC 8.15.1 returns the objects at TOP LEVEL (not under `info`):

```json
{
  "result": 0,
  "env":       { ...same shape as getenvs infos[].env... },
  "nodes":     [ { "id": 47875, "nodeGroup": "cp", "nodeType": "...",
                   "displayName": "...", "status": 1, "fixedCloudlets": ...,
                   "flexibleCloudlets": ..., "intIP": "...", "url": "...",
                   "ismaster": true, "vType": "CT", "type": "NATIVE", ... } ],
  "nodeGroups":[ { "name": "cp", "displayName": "...", "templateType": "NATIVE",
                   "scalingMode": "STATELESS", "vType": "CT",
                   "deployments": [...], "features": ["FIREWALL"], ... } ],
  "envGroups": []
}
```

Client rule: accept BOTH shapes — read `d.get("info", d)` before indexing
`env`/`nodes`/`nodeGroups`. `env.status` uses the classic Jelastic enum
(1 = running, 2 = stopped/sleeping, 4 = ...; treat unknown values as
"not running").

## Result codes observed

| Code | Meaning | Where |
|---|---|---|
| 0 | success | all |
| 501 | invalid parameter (e.g. login not an email) | signin |
| 701 | authentication failed | signin (password auth disabled for SSO accounts) |
| 1702 | unknown/unsupported method | getaccountinfo |
| 2212 | environment is already being deleted | deleteenv retry (from docs) |

## Platform identity

- Jelastic-family API: `source: "hx-core"` in error payloads.
- Panel bundle: `cmp.<host>/applications` (SPA `window.App` config exposes
  `APP_API_URL`, `APP_PLATFORM_VERSION`, SSO settings) — useful for discovery
  of API base URL from the panel host alone.

## Implications for the product

- `virtuozzo-am-mcp login` must implement the Keycloak SSO flow (or capture the
  post-login platform session), not REST password signin.
- The mock should emulate both `getenvs` (nested) and `getenvinfo` (flat +
  nested fallback) shapes and the result codes above.

## Live e2e-verified shapes (2026-11-24, `scripts/e2e_demo.sh`, VHC 8.15.1)

Verified by a full clean end-to-end run (create → deploy → DB node → scale →
logs → agent chat → delete, 12 ok / 0 failed). This is the authoritative spec
for the tool layer's live compatibility.

### Request encoding

- **Form-encoded POST only.** A JSON request body yields
  `invalid parameter [session]`. Nested objects (`env`, `nodes`, `vars`,
  `data`) go in form fields as **JSON strings** (e.g. `nodes=[{...}]`).

### Environment lifecycle

- **createenvironment** works with native stacks:
  `env={"shortdomain": <name>}` +
  `nodes=[{"nodeType":"nginxphp-dockerized","nodeGroup":"cp","count":1,"fixedCloudlets":1,"flexibleCloudlets":3}]`.
  Real `nodeType` values from `gettemplates`: `nginxphp-dockerized`, `lemp`,
  `tomcat`, `nodejs`, `mysql`, `mariadb-dockerized`, `postgresql`, `redis`,
  `mongodb-dockerized`, `docker`, … Env domain: `<env>.eu1.paas.virtuozzo.cloud`.
- **deleteenv** requires `passphrase=<envName>` (typed confirmation) **plus**
  the account password. Without the passphrase: result **724**
  ("Confirmation failed"); the account password alone is not enough.
  A ghost/nonexistent env returns result **11** (not 2024).
- Deleting is fire-and-forget; "confirmed gone" = a later lookup returns 11.

### Deployment

- **deployarchive** wants `fileUrl` + `fileName` (NOT `url`/`archive`);
  extra params are tolerated. Verified sample:
  `fileUrl=https://codeload.github.com/octocat/Spoon-Knife/zip/refs/heads/main`,
  `fileName=app.zip` — deployed and HTTP 200 served.
- Only **jem-capable stacks** accept `deployarchive` (e.g.
  `nginxphp-dockerized` ✅). Plain Docker containers (e.g. `nginx:alpine`)
  fail result **4099** `jem: not found` — no deploy agent inside.

### Topology & scaling

- **changetopology** requires an env descriptor + nodes JSON:
  `env={"region":"","sslstate":false}` + `nodes=[...]`, form-encoded. A plain
  `envName` gives `invalid parameter [env]`. Adding
  `{"nodeType":"mariadb-dockerized","nodeGroup":"db",...}` works. The
  dashboard SPA posts the same body to
  `/JElastic/env/control/rest/changetopology`; the `/1.0/environment/control/rest/`
  base accepts it too.
- **applynodegroupdata** scales an **existing** group:
  `nodeGroup=<name>` + `data={"count":..,"fixedCloudlets":..,"flexibleCloudlets":..}`.
- **editnodegroup** fails with **1102** ("Invalid nodeGroup data") when adding
  nodes — the dashboard uses `changetopology` for adds. Do the same.

### Read surface

- **getsumstat** works; **getenvstat** 404s on VHC. Do not use it.
- **getenvinfo** returns flat top-level `env`/`nodes`/`nodeGroups`,
  `status: 1` = running (numeric), **no `statusName`** field.
- **Logs**: `getlogs` requires `nodeId` and returns a keyword/file listing;
  log **content** comes from **readlog** `path=/var/log/...&line=N` →
  `{"linescount": N, "body": "..."}`. `getlogslist` lists log directories.

### Environment variables

- `getenvvariables`/`setenvvariables` **do not exist** on VHC. Use:
  - read: `getcontainerenvvars(nodeId)` or `getcontainerenvvarsbygroup(nodeGroup)` (GET) → `{"object": {"vars": {...}}}`
  - write: `setcontainerenvvarsbygroup` with form `data={"vars":{...}}`, or
    `addcontainerenvvars` with `vars={"K":"V"}` (both verified `result: 0`).

### Result codes observed live

| Code | Meaning |
|---|---|
| 0 | success |
| 3 | invalid parameter |
| 11 | environment not found (ghost env) |
| 705 | no rights |
| 724 | deleteenv confirmation failed (missing `passphrase=<envName>`) |
| 1102 | invalid nodeGroup data (`editnodegroup` add) |
| 2002 | (observed on failed ops) |
| 2314 | domain already taken |
| 2391 | (observed) |
| 4099 | deploy failed (`jem: not found` — non-jem Docker container) |

### Client rules (live-compat)

1. Always POST form-encoded; nested values as JSON strings in form fields.
2. Walk nested `response` error chains: an outer `result: 0` may carry
   `{response: {result: 3, ...}}` — the innermost non-zero result is the
   real error.
3. Omit `region` unless the caller sets it explicitly — VHC rejects
   `"default"`.
