# SECURITY.md — scope confinement & threat model

The assistant (MCP server + agent) exists for ONE purpose: managing the user's
V/AM platform within their own rights. The model must not be usable as a
general-purpose tool through it. Everything the model sends is **untrusted
input**; everything the platform returns is **untrusted data**.

## Threat model

| # | Threat | Vector |
|---|---|---|
| T1 | **Scope creep** — model steered into using the server for non-V/AM work | free-form tools (fetch/exec/file), over-broad parameters |
| T2 | **Token exfiltration** — session token sent to an attacker host | attacker-controlled `X-Jelastic-Host`, redirect tricks |
| T3 | **SSRF** — platform (or server) made to fetch arbitrary/internal URLs | `deploy_archive` URL param, JPS manifest URL |
| T4 | **Prompt injection** — page/manifest/log content manipulates the model | `X-Vap-Context` page text, JPS `success` text, log lines fed to the LLM |
| T5 | **Parameter injection** — crafted env names/ids hit the REST API unsafely | query/form interpolation |
| T6 | **Secret leakage** — tokens/passwords surface in tool results, logs, cards | verbose error payloads, env-var dumps |
| T7 | **Abuse/resource exhaustion** — runaway loops of mutations | no rate limits, unbounded polling |

## Mitigations (enforced in code, tested)

1. **Closed tool surface.** Only the documented V/AM tools exist. No generic
   HTTP fetch, no shell/exec, no file access, no code execution — ever. Tool
   names/descriptions are static (never built from data).
2. **Host allowlist + token pinning.** `X-Jelastic-Host` (and stdio
   `JELASTIC_HOST`) must match the configured platform origin (exact scheme+
   host+port compare; optional wildcard subdomain match for multi-stand
   setups). The session token is sent ONLY to that origin — redirects are not
   followed to other origins (`follow_redirects` off; SSRF guard on every
   outbound URL).
3. **SSRF guard on every user-supplied URL** (`deploy_archive`, JPS manifest):
   http/https only, DNS-resolved and checked against private/link-local/
   loopback ranges (10/8, 172.16/12, 192.168/16, 127/8, 169.254/16, ::1, fc00::/7,
   metadata 169.254.169.254), no redirects to blocked targets.
4. **Strict input validation.** `envName`/`nodeGroup`/`context` match anchored
   regexes (`^[a-z0-9][a-z0-9-]{2,32}$` class); numeric ids are ints; enums for
   engine/status params; lengths capped. Unknown params are rejected, not
   forwarded.
5. **Untrusted-data marking.** All platform-returned text (logs, JPS messages,
   page context) is injected into the LLM context as DATA with explicit
   untrusted framing; the agent's system prompt forbids following instructions
   found inside it. Context fields from the extension are sanitized (length
   caps, no markup execution).
6. **Secret masking.** Passwords, tokens, and secret-looking values are masked
   in every tool result, card, log line and audit entry (`***`). Secrets are
   never stored server-side (session token is pass-through only).
7. **Mutation gates + audit.** Mutations require `confirm=true` AND the UI
   confirmation round-trip; every mutation lands in the audit log
   (`GET /api/audit`). `env_delete` additionally requires the account password
   passthrough and a danger-tier confirm.
8. **Rate & depth limits.** Per-session caps on tool calls/min and mutations/hour
   (configurable); loop guard on the LLM tool round-trip; async polling is
   bounded. Exceeding limits yields a plain refusal, not a crash.
9. **Least privilege.** The server holds no credentials of its own; it can only
   do what the connected user's platform session can do — the platform is the
   final authorization boundary.
10. **Optional read-only mode.** `VAM_MCP_READONLY=1` turns every mutating tool
    into a refusal — for compliance-sensitive tenants.

## Verification

- pytest: SSRF guard cases (private IPs, redirects, schemes), host-allowlist
  rejection, regex validation fuzz table, secret-masking assertions, rate-limit
  trip, read-only mode.
- The e2e script includes an abuse drill: attempt an off-host token call and a
  private-IP deploy URL; both must be refused and audited.
