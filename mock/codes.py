"""Result and status codes for the mock Jelastic REST API.

Result codes are faithful to the real Jelastic/VAP catalog where documented
(see docs/PLAN.md references): 0 success, 1 unknown error, 9/501 session
invalid, 701 authentication failed, 2212 deletion already in progress,
2314 domain not available, 4109 operation failed, 4132 execution timed out,
11001 JPS manifest corrupted.

Codes marked "mock-defined" have no verified real-stand equivalent; they are
stable identifiers for this mock so tests and the client can map them without
scraping error text.
"""

# --- Result codes (real catalog) -------------------------------------------
OK = 0
UNKNOWN_ERROR = 1                 # unknown error / authorization failed
SESSION_EXPIRED_LEGACY = 9        # session expired (older stands)
SESSION_INVALID = 501             # session missing/unknown/expired (T2 spec)
AUTH_FAILED = 701                 # authentication failed (bad login/password)
DELETE_IN_PROGRESS = 2212         # environment is already being deleted
DOMAIN_UNAVAILABLE = 2314         # domain/env name not available
OPERATION_FAILED = 4109           # installation/operation failed
EXECUTION_TIMEOUT = 4132          # JPS cmd action exceeded time limit
MANIFEST_CORRUPTED = 11001        # JPS manifest YAML corrupted / not parsed

# --- Result codes (mock-defined) -------------------------------------------
ENV_NOT_FOUND = 2024              # mock-defined: environment does not exist
NODE_NOT_FOUND = 2025             # mock-defined: node does not exist
INVALID_PARAMETER = 2026          # mock-defined: missing/invalid parameter

# Session problems the client must treat as "re-authenticate".
SESSION_CODES = (SESSION_INVALID, SESSION_EXPIRED_LEGACY)

# --- Environment status codes ---------------------------------------------
# 0/1/2 match the real catalog (getenvinfo status: 1 running / 2 sleep /
# 0 stopped). 3-7 are mock extensions for observable async transitions.
STATUS_STOPPED = 0
STATUS_RUNNING = 1
STATUS_SLEEP = 2
STATUS_CREATING = 3
STATUS_DELETING = 4
STATUS_STARTING = 5
STATUS_STOPPING = 6
STATUS_SLEEPING = 7

STATUS_CODES = {
    "stopped": STATUS_STOPPED,
    "running": STATUS_RUNNING,
    "sleep": STATUS_SLEEP,
    "creating": STATUS_CREATING,
    "deleting": STATUS_DELETING,
    "starting": STATUS_STARTING,
    "stopping": STATUS_STOPPING,
    "sleeping": STATUS_SLEEPING,
}

STATUS_NAMES = {v: k for k, v in STATUS_CODES.items()}

# Transient states: a transition is in flight.
TRANSIENT_STATES = frozenset({"creating", "deleting", "starting", "stopping", "sleeping"})

# Stable states a lifecycle operation can target.
STABLE_STATES = frozenset({"stopped", "running", "sleep"})
