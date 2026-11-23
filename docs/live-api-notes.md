# Live platform API notes (verified 2026-11-23 against VHC 8.15.1)

Verified facts from live probing of a VHC/Jelastic-family panel. Account
identifiers are redacted. These shapes are what `mcp/jelastic_client.py` and
the mock must honor.

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
