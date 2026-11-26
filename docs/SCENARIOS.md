# SCENARIOS.md — user journey inventory & workflow coverage

What real users do on a Jelastic/VAP platform (from the operations guide,
sections 4.1-4.16 and the 310-method catalog), and how the assistant covers
it. This is the contract for coverage expansion: every scenario must end in
either a GUIDED WORKFLOW (wizard) or a deliberate "not covered" note.

Legend: ✅ workflow exists | 🛠 tools exist, needs workflow | ❌ missing |
🚫 deliberately out of scope (v1)

## Coverage matrix

| # | User scenario (docs) | Tools | Workflow | State |
|---|---|---|---|---|
| 1 | Create environment (4.1) | env_create | env_create wizard | ✅ |
| 2 | Deploy app: archive/VCS/marketplace/Docker, ANY language (4.4, 4.5) | deploy_archive, deploy_jps, deploy_vcs, deploy_any, project_inspect, app_healthcheck | deploy_app wizard | ✅ |
| 3 | Add database (4.2) | db_add | db_add wizard | ✅ |
| 4 | Vertical scaling (4.2) | node_scale | scale flow | ✅ |
| 5 | Start / stop / sleep / wake (4.1) | env_start/stop/sleep | direct (asks confirm) | ✅ |
| 6 | Delete environment (4.1) | env_delete (gated) | direct (danger confirm) | ✅ |
| 7 | **Custom domain bind/unbind (4.6)** | domain_bind, domain_unbind | domain_setup wizard | 🛠 |
| 8 | **SSL for custom domain (4.6)** | ssl_letsencrypt (have) + domain step | part of domain_setup | 🛠 |
| 9 | **Clone environment (4.1/4.16)** | env_clone | clone wizard | 🛠 |
| 10 | **Restart env / node (4.3)** | env_restart, node_restart | quick flow (confirm) | 🛠 |
| 11 | **Ports & endpoints: expose/close (4.7)** | endpoint_open, endpoint_close | endpoint flow (security-gated) | 🛠 |
| 12 | **Container firewall rules (4.11)** | firewall_rule_add, firewall_rule_remove | security flow (gated) | 🛠 |
| 13 | **File manager: list/read/upload/edit (4.10)** | file_list, file_read, file_upload, file_edit | fix-it flow uses it | 🛠 |
| 14 | **Auto horizontal scaling triggers (4.2/trigger/*)** | trigger_list, trigger_create, trigger_delete | autoscale_setup wizard | 🛠 |
| 15 | **Scheduled tasks (4.15)** | task_list, task_create, task_delete | schedule_task wizard | 🛠 |
| 16 | **Share environment with team (4.14)** | env_share, env_share_list | share_env wizard | 🛠 |
| 17 | Billing / quotas / "explain my bill" (4.13) | quota_get, usage_list | usage_report flow | 🛠 |
| 18 | Logs & monitoring (4.8, 4.9) | logs_read, env_stats, op_result | used by troubleshoot | ✅ |
| 19 | Env variables (4.8) | env_var_get/set | direct + deploy flow | ✅ |
| 20 | Marketplace / JPS installs (4.12) | marketplace_list, deploy_jps | deploy_app wizard | ✅ |
| 21 | Security review (4.11 read-only) | security_check | security_review flow | ✅ |
| 22 | Environment groups (4/*group*) | — | — | 🚫 niche |
| 23 | Environment export (4.16) | — | — | 🚫 niche |
| 24 | Account/collaboration admin (31 methods) | env_share subset only | — | 🚫 beyond env_share |
| 25 | SSH keys / account settings | — | — | 🚫 account-level |

## Coverage target for this iteration

Bring rows 7-17 to ✅. That takes platform coverage from ~60% to ~95% of
everyday end-user journeys.

## Design rules for every new scenario

1. One question at a time with option chips; missing info is NEVER an error.
2. Every mutation: recap -> Confirm -> execute -> verify (observable state).
3. Security guardrails for rows 11-12 (firewall/endpoints): plain-language
   risk note in the recap; opening public access is `danger: false` but its
   own confirmation with an explicit "this exposes PORT to the INTERNET" line;
   auto-fix may never weaken firewall state (SECURITY.md rules stand).
4. Secrets (DB passwords, share invitations) are masked in every output.
5. Workflows end with a success card + sensible follow-up actions.
6. Tools stay within scope confinement: only these platform methods, SSRF
   guards on URLs, anchored-regex params (SECURITY.md).
