"""T4 acceptance: guided flows end-to-end with FakeLLM vs the mock.

Every test drives the REAL FastAPI stack over HTTP semantics (ASGI): the
FakeLLM emits ``vap`` directives, the wizards ask clarifying questions
(``ask_user``) and require ``confirm_action`` before any mutation, answers
come through ``POST /api/answer``, and the whole exchange is asserted
against the frozen SSE contract (docs/API-CONTRACT.md v1.1).

The platform is the in-process mock (no network, per tests/conftest.py) and
the deploy-verify step ("curl the env URL") runs against a fake verifier
whose calls are recorded and asserted.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
from fastapi.testclient import TestClient

import agent.main as agent_main
from agent.config import Settings
from agent.events import EVENT_SCHEMA
from mcp.tools import TOOLS
from tests.helpers import TRANSITION, run

#: tools that require an approved confirm_action before they may run
MUTATING = {name for name, spec in TOOLS.items() if spec.mutating}

CARD_ACTION_VERBS = ("auto_fix", "retry", "open_logs", "open_url", "custom")


# --- doubles ----------------------------------------------------------------

class FakeLLM:
    """Scripted OpenAI-compatible LLM: returns queued replies, records calls."""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list = []

    async def complete(self, messages, **kwargs) -> str:
        self.calls.append([dict(m) for m in messages])
        if not self.replies:
            return 'Done.\n```vap\n{"action": "finish"}\n```'
        return self.replies.pop(0)


class FakeVerifier:
    """Records verify step URLs and returns a scripted verdict."""

    def __init__(self, ok: bool = True, status: int = 200, detail: str = ""):
        self.ok = ok
        self.status = status
        self.detail = detail
        self.calls: list = []

    async def __call__(self, url: str) -> dict:
        self.calls.append(url)
        return {"ok": self.ok, "status": self.status, "detail": self.detail}


# --- LLM reply builders (the directive protocol) ----------------------------

def llm_flow(flow: str, params: dict, narration: str = "On it!") -> str:
    directive = {"action": "start_flow", "flow": flow, "params": params}
    return f"{narration}\n```vap\n{json.dumps(directive)}\n```"


def llm_tool(name: str, args: dict, narration: str = "Running it.") -> str:
    directive = {"action": "call_tool", "name": name, "args": args}
    return f"{narration}\n```vap\n{json.dumps(directive)}\n```"


def llm_ask(question: str, options: list, allow_free: bool = True) -> str:
    directive = {"action": "ask_user", "question": question,
                 "options": options, "allow_free": allow_free}
    return f"Let me check.\n```vap\n{json.dumps(directive)}\n```"


FINISH = 'All done.\n```vap\n{"action": "finish"}\n```'


# --- SSE parsing + contract checks ------------------------------------------

def parse_sse(text: str) -> list:
    events = []
    for block in text.split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        name = None
        payload = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: "):].strip()
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: "):])
        if name is not None:
            events.append((name, payload if payload is not None else {}))
    return events


def assert_contract(events: list) -> None:
    """Assert the stream matches docs/API-CONTRACT.md v1.1 exactly."""
    names = [name for name, _ in events]
    assert names, "the stream must carry events"

    # every event name + payload keys match the frozen contract table
    for name, payload in events:
        assert name in EVENT_SCHEMA, f"unknown event [{name}]"
        assert set(payload) == set(EVENT_SCHEMA[name]), \
            f"event [{name}] payload keys {sorted(payload)} != contract " \
            f"{sorted(EVENT_SCHEMA[name])}"

    # exactly one terminal event, and it is last (done | error)
    terminals = [name for name in names if name in ("done", "error")]
    assert terminals, "stream must end with done or error"
    assert len(terminals) == 1, f"multiple terminal events: {terminals}"
    assert names[-1] in ("done", "error"), "done/error must be the last event"

    # tool_call always precedes its tool_result
    seen = {}
    for name, payload in events:
        if name == "tool_call":
            assert payload["call_id"] not in seen
            seen[payload["call_id"]] = False
        elif name == "tool_result":
            assert payload["call_id"] in seen, "tool_result without tool_call"
            seen[payload["call_id"]] = True
    assert all(seen.values()), "every tool_call must be followed by a result"

    # ask_user / confirm_action always precede any tool_call they gate:
    # a mutating tool may only run after some confirm_action
    confirmed_at = -1
    for index, (name, payload) in enumerate(events):
        if name == "confirm_action":
            confirmed_at = index
        elif name == "tool_call" and payload["name"] in MUTATING:
            assert 0 <= confirmed_at < index, \
                f"mutating [{payload['name']}] ran without a preceding " \
                f"confirm_action"

    # raw JSON must never reach the user
    for name, payload in events:
        if name == "token":
            text = payload["text"]
            assert "```" not in text, f"directive block leaked: {text!r}"
            assert '"action"' not in text, f"raw JSON leaked: {text!r}"
            assert '"confirm"' not in text, f"raw JSON leaked: {text!r}"

    # well-formed ask/confirm/card payloads
    for name, payload in events:
        if name == "ask_user":
            assert isinstance(payload["options"], list)
            assert isinstance(payload["allow_free"], bool)
            assert payload["ask_id"].startswith("ask-")
        elif name == "confirm_action":
            assert isinstance(payload["actions"], list)
            assert isinstance(payload["danger"], bool)
            assert payload["confirm_id"].startswith("confirm-")
        elif name == "card":
            assert payload["kind"] in ("success", "info", "error", "security")
            for action in payload["actions"]:
                assert set(action) == {"label", "url", "action", "payload"}
                assert action["action"] in CARD_ACTION_VERBS
        elif name == "progress":
            assert payload["call_id"]
        elif name == "tool_call":
            assert payload["call_id"]


# --- agent-under-test harness -----------------------------------------------

def make_agent(mock_app, *, llm, verifier=None,
               settings=None):
    settings = settings or Settings(llm_base_url="http://llm.test/v1",
                                    llm_api_key="test-key",
                                    llm_model="fake-model")
    return agent_main.create_app(
        settings, llm_factory=lambda: llm,
        transport=httpx.ASGITransport(app=mock_app),
        verifier=verifier)


def make_env(state, name: str) -> str:
    """Create an environment in the mock and let it reach running."""
    code, payload = state.signin(state.config.login, state.config.password)
    assert code == 0, payload
    token = payload["session"]
    code, payload = state.createenvironment(
        token, {"shortdomain": name}, None)
    assert code == 0, payload
    time.sleep(TRANSITION + 0.02)
    state.advance_all()
    return token


def scenario_headers(token: str) -> dict:
    return {"X-Jelastic-Host": "https://mock.local",
            "X-Jelastic-Session": token}


def _client(agent_app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_app),
                             base_url="http://agent.test",
                             timeout=httpx.Timeout(60.0))


async def wait_pending(registry, conversation_id, task):
    """Wait until the turn parks on ask/confirm/card_action."""
    for _ in range(3000):
        conv = registry.get(conversation_id)
        pending = conv.pending if conv is not None else None
        if pending is not None and not pending.future.done():
            return pending
        assert not task.done(), \
            "the turn ended while a scripted answer was still expected"
        await asyncio.sleep(0.01)
    raise AssertionError("turn never parked on a question/confirmation")


async def drive_chat(agent_app, conversation_id: str, message: str,
                     script: list, headers: dict,
                     context: dict = None) -> list:
    """Run one chat turn, answering pauses from ``script`` in order.

    script items: {"kind": "ask", "answer": ...}
                | {"kind": "confirm", "approved": bool}
                | {"kind": "card", "action": ..., "payload": {...}}
                  (payload {"repair_plan_id": "latest"} resolves to the most
                  recently registered repair plan)
    """
    registry = agent_app.state.registry
    hdrs = dict(headers)
    if context is not None:
        hdrs["X-Vap-Context"] = json.dumps(context)
    async with _client(agent_app) as client:
        task = asyncio.create_task(client.post(
            "/api/chat",
            json={"message": message, "conversation_id": conversation_id},
            headers=hdrs))
        for item in script:
            pending = await wait_pending(registry, conversation_id, task)
            expected = "card_action" if item["kind"] == "card" else item["kind"]
            assert pending.kind == expected, \
                f"expected pause [{item['kind']}], got [{pending.kind}]"
            if pending.kind == "ask":
                body = {"conversation_id": conversation_id,
                        "ask_id": pending.ident, "answer": item["answer"]}
            elif pending.kind == "confirm":
                body = {"conversation_id": conversation_id,
                        "confirm_id": pending.ident,
                        "approved": item.get("approved", True)}
            else:
                payload = dict(item.get("payload") or {})
                if payload.get("repair_plan_id") == "latest":
                    conv = registry.get(conversation_id)
                    plans = list(conv.repair_plans) if conv else []
                    assert plans, "no repair plan was registered"
                    payload["repair_plan_id"] = plans[-1]
                body = {"conversation_id": conversation_id,
                        "action": item.get("action", "auto_fix"),
                        "payload": payload}
            response = await client.post("/api/answer", json=body,
                                         headers=headers)
            assert response.status_code == 200, response.text
        response = await asyncio.wait_for(task, 60)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        return parse_sse(response.text)


def tool_calls(events, name=None):
    return [payload for event, payload in events
            if event == "tool_call"
            and (name is None or payload["name"] == name)]


def cards(events, kind=None):
    return [payload for event, payload in events
            if event == "card" and (kind is None or payload["kind"] == kind)]


def texts(events) -> str:
    return "".join(payload["text"] for event, payload in events
                   if event == "token")


# --- flow 1: deploy wizard ---------------------------------------------------

def test_deploy_wizard_end_to_end(app, state):
    token = make_env(state, "existing-1")
    llm = FakeLLM(
        llm_flow("deploy_app", {
            "source": "archive", "url": "https://example.com/app.tar.gz",
            "stack": "nodejs:20", "db": "mysql:8.0", "cloudlets": 4}),
        FINISH)
    verifier = FakeVerifier()
    agent_app = make_agent(app, llm=llm, verifier=verifier)

    async def main():
        events = await drive_chat(
            agent_app, "conv-deploy", "Please deploy my app",
            [{"kind": "ask", "answer": "Create a new environment"},
             {"kind": "ask", "answer": "demo-app"},
             {"kind": "ask", "answer": "Yes, enable SSL"},
             {"kind": "confirm", "approved": True}],
            scenario_headers(token),
            context={"envName": "existing-1", "page": "dashboard",
                     "url": "https://mock.local/"})
        async with _client(agent_app) as client:
            ctx = await client.get("/api/context",
                                   params={"conversation_id": "conv-deploy"})
            audit = await client.get("/api/audit",
                                     params={"conversation_id": "conv-deploy"})
        return events, ctx.json(), audit.json()

    events, context_body, audit_body = run(main())
    assert_contract(events)

    # questions asked (one at a time, with options), confirmation required
    asks = [p for name, p in events if name == "ask_user"]
    assert len(asks) == 3
    # multiple-choice questions carry option chips (free-text ones may be [])
    assert sum(1 for ask in asks if ask["options"]) >= 2
    confirms = [p for name, p in events if name == "confirm_action"]
    assert len(confirms) == 1
    assert "demo-app" in confirms[0]["summary"]

    # tool sequence: create -> wait -> deploy -> ssl -> verify
    names = [p["name"] for name, p in events if name == "tool_call"]
    for expected in ("env_create", "op_result", "deploy_archive",
                     "ssl_letsencrypt", "verify_http"):
        assert expected in names, f"missing step [{expected}]: {names}"
    assert names.index("env_create") < names.index("deploy_archive")

    # the verify step ran against the environment URL
    assert verifier.calls == ["https://demo-app.mock.jelastic.local/"]
    verify_result = [p for name, p in events if name == "tool_result"
                     and p["summary"].startswith(
                         "https://demo-app.mock.jelastic.local/")]
    assert verify_result and verify_result[0]["ok"] is True

    # rich results: success card + security review card; done last
    success = cards(events, "success")
    assert success and "demo-app" in success[0]["body_md"]
    assert any(a["action"] == "open_url" for a in success[0]["actions"])
    security = cards(events, "security")
    assert security, "the deploy wizard must end with a security review"
    assert "No SSL" not in security[0]["body_md"], \
        "SSL was enabled — the review must not flag it"
    assert events[-1][0] == "done"

    # the platform really changed
    env = state.envs["demo-app"]
    assert env.status == "running"
    assert env.ssl is not None
    assert env.app is not None and env.app["context"] == "ROOT"
    db_nodes = [n for n in env.nodes if n.node_group == "db"]
    assert db_nodes and db_nodes[0].node_type == "mysql"

    # context + audit endpoints (contract v1.1)
    assert context_body["context"] == {"envName": "existing-1",
                                       "page": "dashboard",
                                       "url": "https://mock.local/"}
    audit_tools = [entry["tool"] for entry in audit_body]
    assert audit_tools == ["env_create", "deploy_archive", "ssl_letsencrypt"]
    assert all(entry["ok"] for entry in audit_body)
    assert all("test-key" not in json.dumps(entry) for entry in audit_body)


def test_deploy_confirmation_rejected_changes_nothing(app, state):
    token = make_env(state, "existing-1")
    llm = FakeLLM(
        llm_flow("deploy_app", {
            "source": "archive", "url": "https://example.com/app.tar.gz",
            "env_name": "cancel-app", "stack": "php:8.3", "db": "",
            "cloudlets": 4, "ssl": True}),
        FINISH)
    verifier = FakeVerifier()
    agent_app = make_agent(app, llm=llm, verifier=verifier)

    events = run(drive_chat(
        agent_app, "conv-cancel", "Deploy please",
        [{"kind": "confirm", "approved": False}],
        scenario_headers(token)))
    assert_contract(events)

    # confirmation refused -> narration of the abort, no mutation at all
    assert "cancel-app" not in state.envs
    assert not any(p["name"] in MUTATING for name, p in events
                   if name == "tool_call")
    assert "changed nothing" in texts(events)
    assert events[-1][0] == "done"


# --- flow 2: add database ----------------------------------------------------

def test_db_add_wizard_end_to_end(app, state):
    token = make_env(state, "web1")
    llm = FakeLLM(
        llm_flow("db_add", {"env_name": "web1", "engine": "mysql",
                            "cloudlets": 4}),
        FINISH)
    agent_app = make_agent(app, llm=llm, verifier=FakeVerifier())

    events = run(drive_chat(
        agent_app, "conv-db", "Add a database",
        [{"kind": "ask", "answer": "8.0"},
         {"kind": "ask", "answer": "Generate automatically (recommended)"},
         {"kind": "confirm", "approved": True}],
        scenario_headers(token)))
    assert_contract(events)

    asks = [p for name, p in events if name == "ask_user"]
    assert len(asks) == 2
    confirms = [p for name, p in events if name == "confirm_action"]
    assert len(confirms) == 1 and "mysql" in confirms[0]["summary"].lower()

    names = [p["name"] for name, p in events if name == "tool_call"]
    assert "db_add" in names and "op_result" in names

    success = cards(events, "success")
    assert success and "mysql://web1.mock.jelastic.local:3306" \
        in success[0]["body_md"]
    # the account password never appears in anything shown to the user
    assert state.config.password not in texts(events)

    db_nodes = [n for n in state.envs["web1"].nodes if n.node_group == "db"]
    assert db_nodes and db_nodes[0].node_type == "mysql"
    assert events[-1][0] == "done"


# --- flow 3: scale -----------------------------------------------------------

def test_scale_flow_end_to_end(app, state):
    token = make_env(state, "busy")
    node_id = state.envs["busy"].nodes[0].id
    llm = FakeLLM(llm_flow("scale", {"env_name": "busy"}), FINISH)
    agent_app = make_agent(app, llm=llm, verifier=FakeVerifier())

    events = run(drive_chat(
        agent_app, "conv-scale", "Make it faster",
        [{"kind": "ask", "answer": "Scale to 12 cloudlets (Prod)"},
         {"kind": "confirm", "approved": True}],
        scenario_headers(token)))
    assert_contract(events)

    scale_calls = tool_calls(events, "node_scale")
    assert scale_calls and scale_calls[0]["args"]["node_id"] == node_id
    assert scale_calls[0]["args"]["fixed_cloudlets"] == 1
    assert scale_calls[0]["args"]["flexible_cloudlets"] == 11

    # stats are read before AND after the change
    names = [p["name"] for name, p in events if name == "tool_call"]
    first_stats = names.index("env_stats")
    scale_at = names.index("node_scale")
    second_stats = names.index("env_stats", scale_at)
    assert first_stats < scale_at < second_stats

    node = next(n for n in state.envs["busy"].nodes if n.id == node_id)
    assert (node.fixed_cloudlets, node.flexible_cloudlets) == (1, 11)
    assert events[-1][0] == "done"


# --- flow 4: troubleshoot ----------------------------------------------------

def test_troubleshoot_flow_end_to_end(app, state):
    token = make_env(state, "sick")
    code, payload = state.stopenv(token, "sick")
    assert code == 0, payload
    time.sleep(TRANSITION + 0.02)
    state.advance_all()

    llm = FakeLLM(llm_flow("troubleshoot", {"env_name": "sick"}), FINISH)
    agent_app = make_agent(app, llm=llm, verifier=FakeVerifier())

    events = run(drive_chat(
        agent_app, "conv-fix", "It is broken",
        [{"kind": "confirm", "approved": True}],
        scenario_headers(token)))
    assert_contract(events)

    # evidence gathered first (state, usage, logs), then the fix
    names = [p["name"] for name, p in events if name == "tool_call"]
    assert names[:3] == ["env_info", "env_stats", "logs_read"]
    assert "env_start" in names
    assert "stopped" in texts(events)
    time.sleep(TRANSITION + 0.02)   # the platform change is asynchronous
    state.advance_all()
    assert state.envs["sick"].status == "running"
    assert cards(events, "success")
    assert events[-1][0] == "done"


# --- error -> diagnose -> auto-fix -------------------------------------------

def test_error_diagnose_autofix_manifest(app, state):
    token = make_env(state, "shop")
    # a manifest with a mid-string tab character is faithfully rejected by
    # the platform (a trailing one would be masked by argument normalization)
    llm = FakeLLM(
        llm_tool("deploy_jps", {
            "env_name": "shop",
            "jps": '{"jpsType": "install",\t"name": "shop"}'},
            "Installing the package."),
        FINISH)
    agent_app = make_agent(app, llm=llm, verifier=FakeVerifier())

    events = run(drive_chat(
        agent_app, "conv-fixup", "Install it",
        [{"kind": "confirm", "approved": True},        # the failing install
         {"kind": "card", "action": "auto_fix",
          "payload": {"repair_plan_id": "latest"}},    # one-click repair
         {"kind": "confirm", "approved": True}],        # repair recap
        scenario_headers(token)))
    assert_contract(events)

    # the failure produced a diagnosed error card with a one-click auto_fix
    errors = cards(events, "error")
    assert errors, "a failed step must produce an error card"
    primary = errors[0]["actions"][0]
    assert primary["action"] == "auto_fix"
    assert primary["payload"].get("repair_plan_id", "").startswith("repair-")
    assert "manifest" in errors[0]["body_md"].lower()

    # diagnosis gathered evidence before explaining
    names = [p["name"] for name, p in events if name == "tool_call"]
    assert "logs_read" in names

    # the repair re-ran deploy_jps with the manifest sanitized, then verified
    deploy_calls = tool_calls(events, "deploy_jps")
    assert len(deploy_calls) == 2
    assert "\t" in deploy_calls[0]["args"]["jps"]
    assert "\t" not in deploy_calls[1]["args"]["jps"]
    assert cards(events, "success")

    # the package really installed
    assert len(state.envs["shop"].installations) == 1
    assert events[-1][0] == "done"


def test_repair_guardrails_refuse_unsafe_plans():
    from agent.diagnose import repair_plan_is_safe
    assert repair_plan_is_safe({
        "steps": [{"tool": "env_start", "args": {"env_name": "x"}}]})
    assert not repair_plan_is_safe({
        "steps": [{"tool": "env_delete", "args": {"env_name": "x"}}]})
    assert not repair_plan_is_safe({
        "steps": [{"tool": "env_var_set",
                   "args": {"env_name": "x", "vars": {"A": "B"}}}]})
    assert not repair_plan_is_safe({
        "steps": [{"tool": "deploy_jps",
                   "args": {"env_name": "x", "password": "hunter2"}}]})
    assert not repair_plan_is_safe({
        "steps": [{"tool": "ssl_letsencrypt",
                   "args": {"env_name": "x", "ssl": False}}]})
    assert not repair_plan_is_safe({"steps": []})
    assert not repair_plan_is_safe(None)


# --- security & compliance ---------------------------------------------------

def test_security_review_flow_and_one_click_fix(app, state):
    token = make_env(state, "store")
    llm = FakeLLM(
        llm_flow("security_review", {"env_name": "store"}),
        FINISH)
    agent_app = make_agent(app, llm=llm, verifier=FakeVerifier())

    events = run(drive_chat(
        agent_app, "conv-sec", "Check my environment security",
        [{"kind": "card", "action": "auto_fix",
          "payload": {"repair_plan_id": "latest"}},
         {"kind": "confirm", "approved": True}],
        scenario_headers(token)))
    assert_contract(events)

    security = cards(events, "security")
    assert security, "the review must render a security card"
    assert "No SSL" in security[0]["body_md"]
    fix = security[0]["actions"][0]
    assert fix["action"] == "auto_fix"
    assert fix["payload"].get("repair_plan_id", "").startswith("repair-")

    # one-click fix ran through the guarded repair: confirm -> apply -> verify
    confirms = [p for name, p in events if name == "confirm_action"]
    assert confirms, "a fix is still a mutation: it needs a confirmation"
    assert "ssl" in confirms[0]["summary"].lower()
    ssl_calls = tool_calls(events, "ssl_letsencrypt")
    assert ssl_calls and ssl_calls[0]["args"]["ext_domain"] == \
        "store.mock.jelastic.local"
    assert state.envs["store"].ssl is not None
    assert cards(events, "success")
    assert events[-1][0] == "done"


# --- verify step failure is honest -------------------------------------------

def test_verify_failure_reports_honestly(app, state):
    token = make_env(state, "existing-1")
    llm = FakeLLM(
        llm_flow("deploy_app", {
            "source": "archive", "url": "https://example.com/app.tar.gz",
            "env_name": "verify-me", "stack": "php:8.3", "db": "",
            "cloudlets": 4, "ssl": True}),
        FINISH)
    verifier = FakeVerifier(ok=False, status=500, detail="boom")
    agent_app = make_agent(app, llm=llm, verifier=verifier)

    events = run(drive_chat(
        agent_app, "conv-verify", "Deploy please",
        [{"kind": "confirm", "approved": True},
         {"kind": "card", "action": "cancel", "payload": {}}],
        scenario_headers(token)))
    assert_contract(events)

    assert verifier.calls == ["https://verify-me.mock.jelastic.local/"]
    errors = cards(events, "error")
    assert errors, "an unreachable app must produce an error card"
    assert "500" in errors[0]["body_md"]
    # no auto-fix offered when there is no safe repair — honest failure
    assert not any(a["action"] == "auto_fix" for a in errors[0]["actions"])
    assert events[-1][0] == "done"


# --- ask_user directive + SSE event shapes -----------------------------------

def test_ask_user_directive_roundtrip(app, state):
    token = make_env(state, "web1")
    llm = FakeLLM(
        llm_ask("Which environment?", ["web1", "web2"]),
        FINISH)
    agent_app = make_agent(app, llm=llm, verifier=FakeVerifier())

    events = run(drive_chat(
        agent_app, "conv-ask", "Deploy it",
        [{"kind": "ask", "answer": "web1"}],
        scenario_headers(token)))
    assert_contract(events)

    asks = [p for name, p in events if name == "ask_user"]
    assert len(asks) == 1
    assert asks[0]["question"] == "Which environment?"
    assert asks[0]["options"] == ["web1", "web2"]
    assert asks[0]["allow_free"] is True

    # the answer was fed back to the LLM for the next round
    last_call = llm.calls[-1]
    assert {"role": "user", "content": "web1"} in last_call

    # narration streamed as token events
    assert texts(events).strip().endswith("All done.")
    assert events[-1][0] == "done"


# --- mutations always require confirmation -----------------------------------

def test_direct_mutation_declined_runs_nothing(app, state):
    token = make_env(state, "guard")
    llm = FakeLLM(
        llm_tool("env_stop", {"env_name": "guard"}, "Stopping it."),
        FINISH)
    agent_app = make_agent(app, llm=llm, verifier=FakeVerifier())

    events = run(drive_chat(
        agent_app, "conv-guard", "Stop my environment",
        [{"kind": "confirm", "approved": False}],
        scenario_headers(token)))
    assert_contract(events)

    confirms = [p for name, p in events if name == "confirm_action"]
    assert confirms and confirms[0]["actions"] == ["env_stop"]
    assert not tool_calls(events), "a declined confirmation runs no tool"
    assert state.envs["guard"].status == "running"
    assert "changed nothing" in texts(events)
    assert events[-1][0] == "done"


# --- API surface (error codes, healthz) -------------------------------------

def test_api_error_codes_and_healthz(app):
    llm = FakeLLM(FINISH)
    agent_app = make_agent(app, llm=llm, verifier=FakeVerifier())
    client = TestClient(agent_app)

    # missing platform credentials -> 401 no_credentials
    response = client.post("/api/chat", json={"message": "hi"})
    assert response.status_code == 401
    assert response.json()["code"] == "no_credentials"

    # malformed body -> 400 bad_request
    response = client.post("/api/chat", json={"message": ""},
                           headers=scenario_headers("token"))
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"

    # wrong answer target -> 400 bad_request
    response = client.post("/api/answer", json={
        "conversation_id": "nope", "ask_id": "ask-1", "answer": "x"})
    assert response.status_code == 400

    # healthz: configured, never echoes secrets
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["llm_configured"] is True
    assert "test-key" not in response.text
    assert "api_key" not in response.text

    # no LLM configured -> 503 llm_not_configured
    bare = agent_main.create_app(Settings(), transport=None,
                                 verifier=FakeVerifier())
    bare_client = TestClient(bare)
    response = bare_client.post(
        "/api/chat", json={"message": "hi"},
        headers=scenario_headers("token"))
    assert response.status_code == 503
    assert response.json()["code"] == "llm_not_configured"
    assert bare_client.get("/healthz").json()["llm_configured"] is False
