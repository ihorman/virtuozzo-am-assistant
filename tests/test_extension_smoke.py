"""Headless-Chromium smoke test for the MV3 extension (T5 acceptance bar).

Loads the unpacked extension into a real headless Chromium, serves
``mock/dashboard.html`` over loopback, and drives the sidepanel against the
scripted mock agent (``tests/mock_agent.py``). Everything runs on
127.0.0.1 — no real network (the autouse conftest guard stays happy).

Acceptance coverage:
  * FAB renders on the mock dashboard (content script, shadow DOM),
  * side panel chat roundtrip works (send -> streamed SSE reply ->
    ask_user chips -> confirm -> progress -> success card).

The DESIGN.md verification bar is covered too: every state is screenshotted
in dark and light themes into ``tests/screenshots/`` for review.

Container note: this host's cgroup pids cap is 400 tasks, so Chromium runs
with ``--single-process`` (documented in the module LEAN_ARGS).
"""

from __future__ import annotations

import functools
import http.server
import os
import pathlib
import socket
import sys
import threading
import time

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# stretch scenario pauses so in-flight states (progress cards) can be caught
os.environ.setdefault("VAP_MOCK_PAUSE_SCALE", "3.0")

import pytest  # noqa: E402
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from tests.mock_agent import create_mock_agent  # noqa: E402

EXTENSION_DIR = REPO_ROOT / "extension"
SHOTS_DIR = pathlib.Path(os.environ.get(
    "VAP_SHOTS_DIR", str(REPO_ROOT / "tests" / "screenshots")))
DASHBOARD_PATH = "/dashboard.html?env=my-demo-env"

#: chromium flags that fit the container's 400-task cgroup budget
LEAN_ARGS = [
    "--no-sandbox",
    "--single-process",
    "--disable-gpu",
    "--disable-features=site-per-process,AudioServiceOutOfProcess",
    f"--disable-extensions-except={EXTENSION_DIR}",
    f"--load-extension={EXTENSION_DIR}",
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


#: this container's cgroup task cap (threads count); chromium launch fails
#: with `pthread_create: Resource temporarily unavailable` when it is tight
CGROUP_PIDS_MAX = 400
CGROUP_PIDS_CURRENT = "/sys/fs/cgroup/pids/pids.current"


def _task_count() -> int:
    try:
        return int(pathlib.Path(CGROUP_PIDS_CURRENT).read_text().strip())
    except OSError:
        return 0


def _await_task_budget(limit: int = 330, timeout: float = 30.0) -> None:
    """Wait until the container has headroom for a Chromium launch."""
    deadline = time.monotonic() + timeout
    while _task_count() > limit and time.monotonic() < deadline:
        time.sleep(0.5)


# --------------------------------------------------------------------- #
# servers                                                               #
# --------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def agent_server():
    """The scripted mock agent on 127.0.0.1."""
    port = _free_port()
    config = uvicorn.Config(create_mock_agent(), host="127.0.0.1", port=port,
                            log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("mock agent did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def static_server():
    """Serves mock/ (the mock dashboard) on 127.0.0.1."""

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            pass

    handler = functools.partial(QuietHandler, directory=str(REPO_ROOT / "mock"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    thread.join(timeout=5)


# --------------------------------------------------------------------- #
# browser helpers                                                       #
# --------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def playwright_instance():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def profile_dir(tmp_path_factory):
    """One shared profile dir: settings persist across launches."""
    return str(tmp_path_factory.mktemp("vap-profile"))


class Browser:
    """A launched persistent context + the discovered extension id."""

    def __init__(self, ctx, ext_id):
        self.ctx = ctx
        self.ext_id = ext_id

    def page_for(self, rel_path: str, viewport=None):
        page = self.ctx.new_page()
        if viewport:
            page.set_viewport_size(viewport)
        page.goto(f"chrome-extension://{self.ext_id}/{rel_path}")
        return page

    def close(self):
        self.ctx.close()


def launch(playwright_instance, profile_dir, color_scheme="dark") -> Browser:
    """Launch the extension browser, retrying around the container's task cap."""
    last_error = None
    for attempt in range(3):
        _await_task_budget()
        try:
            ctx = playwright_instance.chromium.launch_persistent_context(
                profile_dir,
                channel="chromium",
                headless=True,
                args=LEAN_ARGS,
                color_scheme=color_scheme,
                viewport={"width": 400, "height": 820},
            )
            break
        except Exception as error:          # flaky under task-cap pressure
            last_error = error
            time.sleep(2.0)
    else:
        raise RuntimeError(
            f"chromium launch failed 3x (task budget: {_task_count()}/"
            f"{CGROUP_PIDS_MAX}): {last_error}")
    ctx.set_default_timeout(10_000)
    workers = ctx.service_workers
    if workers:
        ext_id = workers[0].url.split("/")[2]
    else:
        worker = ctx.wait_for_event("serviceworker", timeout=20_000)
        ext_id = worker.url.split("/")[2]
    return Browser(ctx, ext_id)


@pytest.fixture
def browser(playwright_instance, profile_dir):
    browsers = []

    def _launch(color_scheme="dark") -> Browser:
        browser = launch(playwright_instance, profile_dir, color_scheme)
        browsers.append(browser)
        return browser

    yield _launch
    for browser in browsers:
        browser.close()


def shot(page, name: str) -> str:
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOTS_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=False)
    return str(path)


def wait_until(predicate, timeout: float = 15.0, interval: float = 0.1,
               message: str = ""):
    """Poll ``predicate`` (Python callable) until it returns truthy.

    Non-eval replacement for ``page.wait_for_function`` string predicates:
    MV3 extension pages enforce ``script-src 'self'`` (no ``unsafe-eval``),
    so Playwright's string-eval wait raises EvalError on chrome-extension://
    pages. Poll locator reads (``inner_text``/``get_attribute``) instead —
    same pattern as ``tests/helpers.py::wait_status_raw``.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except Exception:  # DOM race during render — keep polling
            last = None
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(
        f"timed out after {timeout}s waiting for {message!r} (last={last!r})")


def configure_agent(browser: Browser, agent_url: str) -> None:
    """Point the extension at the mock agent.

    The agent URL is intentionally not user-facing (Options shows only the
    Platform field), so tests set it through chrome.storage directly."""
    page = browser.page_for("options/options.html", viewport={"width": 900, "height": 800})
    page.evaluate("url => chrome.storage.sync.set({ agentUrl: url })", agent_url)
    page.close()


def open_dashboard(browser: Browser, static_url: str):
    """Open the mock dashboard so the content script captures env context."""
    page = browser.ctx.new_page()
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(static_url + DASHBOARD_PATH)
    return page


def open_sidepanel(browser: Browser):
    return browser.page_for("sidepanel/sidepanel.html", viewport={"width": 400, "height": 820})


# --------------------------------------------------------------------- #
# 1. FAB on the mock dashboard                                          #
# --------------------------------------------------------------------- #

def test_fab_renders_on_mock_dashboard(browser, static_server):
    web = browser()
    page = open_dashboard(web, static_server)

    fab = page.locator("#vap-fab")
    fab.wait_for(state="visible")
    assert "AI Assistant" in fab.inner_text()
    shot(page, "fab-dark")

    fab.click()
    host = page.locator("#vap-copilot-fab-root")
    wait_until(lambda: host.get_attribute("data-opened"),
               message="#vap-copilot-fab-root[data-opened]")
    opened_via = host.get_attribute("data-opened")
    assert opened_via in ("sidePanel", "tab"), f"unexpected open path: {opened_via}"


# --------------------------------------------------------------------- #
# 2. options page                                                       #
# --------------------------------------------------------------------- #

def test_options_page_configures_agent(browser, agent_server):
    web = browser()
    page = web.page_for("options/options.html", viewport={"width": 900, "height": 800})

    # New Options contract: only the Platform field is visible; the agent URL
    # is internal and must NOT be rendered.
    assert page.locator("#panel-host").is_visible()
    assert page.locator("#agent-url").count() == 0, "agent URL must not be user-facing"

    page.fill("#panel-host", "https://panel.example.com")
    shot(page, "options-dark")
    page.click("#btn-save")
    page.wait_for_selector("#save-note:not([hidden])")
    stored = page.evaluate("() => chrome.storage.sync.get('panelHost')")
    assert stored.get("panelHost") == "https://panel.example.com"

    page.evaluate("url => chrome.storage.sync.set({ agentUrl: url })", agent_server)
    page.evaluate("() => { document.querySelector('details.advanced').open = true; }")
    page.click("#btn-health")
    diagnostics = page.locator("#diagnostics")
    wait_until(lambda: "reachable" in diagnostics.inner_text(),
               message="#diagnostics includes 'reachable'")
    assert "llm configured" in page.locator("#diagnostics").inner_text()
    page.close()


# --------------------------------------------------------------------- #
# 3. sidepanel chat roundtrip + deploy wizard (acceptance)              #
# --------------------------------------------------------------------- #

def test_sidepanel_chat_roundtrip_and_deploy_wizard(browser, agent_server, static_server):
    web = browser()
    configure_agent(web, agent_server)
    open_dashboard(web, static_server)          # content script -> env context
    panel = open_sidepanel(web)

    # context bar carries the captured environment
    panel.wait_for_selector("#env-pill:not([hidden])")
    assert panel.locator("#env-name").inner_text() == "my-demo-env"

    # empty state: hero + suggestion chips
    assert panel.locator(".hero").inner_text() == "What are we doing today?"
    assert panel.locator(".chip.suggestion").count() >= 4
    assert panel.locator(".chip.suggestion",
                         has_text="Deploy a new application").count() == 1
    shot(panel, "empty-dark")

    # -- roundtrip: type a message, get a streamed markdown reply --------
    panel.fill("#input", "Hello")
    panel.press("#input", "Enter")
    panel.wait_for_selector(".msg-user")
    panel.wait_for_selector(".msg-assistant .md")
    first_md = panel.locator(".msg-assistant .md").first
    wait_until(lambda: "I'm your" in first_md.inner_text(),
               message=".msg-assistant .md includes \"I'm your\"")
    assert panel.locator(".msg-assistant .md code").count() >= 1   # markdown code
    assert panel.locator(".md-copy").count() >= 1                  # copy buttons
    shot(panel, "hello-dark")

    # -- guided flow: ask -> confirm -> progress -> success --------------
    panel.locator(".chip.suggestion", has_text="Deploy a new application").click()

    panel.wait_for_selector(".card.ask")
    assert panel.locator(".card.ask .steps .step").count() >= 2
    assert panel.locator(".card.ask .ask-options .chip").count() == 3
    assert panel.locator(".card.ask .free-text input").count() == 1
    shot(panel, "ask-dark")

    panel.locator(".card.ask .chip", has_text="Node.js + PostgreSQL").click()
    panel.wait_for_selector(".card.confirm")
    recap = panel.locator(".card.confirm .confirm-recap").inner_text()
    assert "Node.js 20 + PostgreSQL" in recap and "$12/mo" in recap
    assert panel.locator(".card.confirm .confirm-actions-list li").count() == 3
    shot(panel, "confirm-dark")

    panel.locator(".card.confirm .btn.primary").click()
    panel.wait_for_selector(".card.progress")
    panel.wait_for_selector(".card.progress .elapsed")
    shot(panel, "progress-dark")

    panel.wait_for_selector(".card.success")
    success = panel.locator(".card.success")
    assert success.locator(".card-actions .btn").count() == 3      # Open app/dash/logs
    assert success.locator(".mono-value").count() >= 2             # copyable values
    assert success.locator(".mono-value .md-copy").count() >= 2    # copy buttons
    shot(panel, "success-dark")


# --------------------------------------------------------------------- #
# 4. error / security / audit cards                                     #
# --------------------------------------------------------------------- #

def test_error_security_audit_cards(browser, agent_server, static_server):
    web = browser()
    configure_agent(web, agent_server)
    open_dashboard(web, static_server)
    panel = open_sidepanel(web)

    # security card with severity pills + one-click fixes
    panel.fill("#input", "check my environment security")
    panel.press("#input", "Enter")
    panel.wait_for_selector(".card.security")
    security = panel.locator(".card.security")
    assert security.locator("li .pill").count() == 3               # high/medium/low
    assert security.locator(".card-actions .btn").count() == 2     # Fix high / Fix all
    shot(panel, "security-dark")

    # error card with a translated message + Fix it automatically
    panel.fill("#input", "simulate an error")
    panel.press("#input", "Enter")
    panel.wait_for_selector(".card.error")
    error = panel.locator(".card.error")
    assert error.locator(".btn.primary").inner_text() == "Fix it automatically"
    assert "heap out of memory" in error.inner_text()
    shot(panel, "error-dark")

    # auto-fix: recap -> confirm -> progress -> verified success
    error.locator(".btn.primary").click()
    panel.wait_for_selector(".card.confirm")
    shot(panel, "autofix-confirm-dark")
    panel.locator(".card.confirm .btn.primary").click()
    panel.wait_for_selector(".card.success")
    assert "Fixed" in panel.locator(".card.success").inner_text()
    shot(panel, "fixed-success-dark")

    # audit card ("What I changed")
    panel.click("#btn-audit")
    panel.wait_for_selector(".card.audit .timeline-item")
    audit = panel.locator(".card.audit")
    assert "env_var_add" in audit.inner_text()
    assert audit.locator(".pill.ok").count() >= 1
    shot(panel, "audit-dark")


# --------------------------------------------------------------------- #
# 4b. panel background == dashboard background (user feedback)          #
# --------------------------------------------------------------------- #

def test_panel_background_matches_dashboard(browser, agent_server, static_server):
    """The side panel must continue the dashboard background seamlessly.

    The mock dashboard paints its background on a wrapper div (#page-bg),
    not on body — reproducing the real Jelastic/VHC case where naive
    body/html sampling returns transparent."""
    web = browser()
    configure_agent(web, agent_server)
    page = open_dashboard(web, static_server)
    page_bg = page.evaluate(
        "() => getComputedStyle(document.getElementById('page-bg')).backgroundColor")

    panel = open_sidepanel(web)
    panel.wait_for_selector(".hero")
    wait_until(
        lambda: panel.evaluate(
            "() => getComputedStyle(document.body).backgroundColor") == page_bg,
        message=f"panel background == dashboard background ({page_bg})")
    assert panel.evaluate(
        "() => getComputedStyle(document.body).backgroundColor") == page_bg
    panel.close()
    page.close()


# --------------------------------------------------------------------- #
# 4c. VHC-like dashboard detection (user bug: 'auto-detect' + bg mismatch) #
# --------------------------------------------------------------------- #

def test_vhc_like_dashboard_detection_and_context(browser, agent_server,
                                                  static_server):
    """Regression: a VHC-like dashboard (title 'Loading...', no jelastic
    markers, wrapper-div background) must still be detected — so the panel
    shows the platform host (not 'auto-detect') AND continues the exact
    dashboard background."""
    web = browser()
    configure_agent(web, agent_server)
    page = web.ctx.new_page()
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(static_server + "/dashboard-vhc.html")

    fab = page.locator("#vap-fab")
    fab.wait_for(state="visible")

    page_bg = page.evaluate(
        "() => getComputedStyle(document.getElementById('page-bg')).backgroundColor")

    panel = open_sidepanel(web)
    panel.wait_for_selector(".hero")

    # the context bar shows the platform host, never the 'auto-detect' fallback
    wait_until(
        lambda: panel.locator("#host-label").inner_text() != "auto-detect",
        message="context bar shows the platform host")
    assert "127.0.0.1" in panel.locator("#host-label").inner_text()

    # the panel continues the dashboard background seamlessly
    wait_until(
        lambda: panel.evaluate(
            "() => getComputedStyle(document.body).backgroundColor") == page_bg,
        message=f"panel background == dashboard background ({page_bg})")

    panel.close()
    page.close()


# --------------------------------------------------------------------- #
# 5. light theme screenshots (DESIGN.md verification bar)               #
# --------------------------------------------------------------------- #

def test_light_theme_screenshots(browser, agent_server):
    web = browser(color_scheme="light")
    configure_agent(web, agent_server)
    panel = open_sidepanel(web)

    panel.wait_for_selector(".hero")
    shot(panel, "empty-light")

    panel.locator(".chip.suggestion", has_text="Deploy a new application").click()
    panel.wait_for_selector(".card.ask")
    shot(panel, "ask-light")

    panel.locator(".card.ask .chip", has_text="Node.js + PostgreSQL").click()
    panel.wait_for_selector(".card.confirm")
    shot(panel, "confirm-light")

    panel.locator(".card.confirm .btn.primary").click()
    panel.wait_for_selector(".card.progress")
    shot(panel, "progress-light")

    panel.wait_for_selector(".card.success")
    shot(panel, "success-light")

    panel.fill("#input", "simulate an error")
    panel.press("#input", "Enter")
    panel.wait_for_selector(".card.error")
    shot(panel, "error-light")
