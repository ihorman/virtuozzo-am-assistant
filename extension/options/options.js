/* VAP Copilot options page logic: settings + session fallback + diagnostics. */
"use strict";

(() => {
  const $ = (sel) => document.querySelector(sel);
  const fields = {
    agentUrl: $("#agent-url"),
    panelHost: $("#panel-host"),
    sessionToken: $("#session-token"),
  };

  function rpc(message) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(message, (reply) => resolve(reply || { ok: false }));
      } catch (err) {
        resolve({ ok: false, error: String(err) });
      }
    });
  }

  function setSessionStatus(text, ok) {
    const node = $("#session-status");
    node.textContent = text;
    node.style.color = ok ? "var(--ok)" : "var(--muted)";
  }

  async function load() {
    const reply = await rpc({ type: "get-settings" });
    if (reply.ok) {
      fields.agentUrl.value = reply.settings.agentUrl || "";
      fields.panelHost.value = reply.settings.panelHost || "";
      setSessionStatus(
        reply.hasSession
          ? "Session token: captured ✓"
          : "Session token: not captured yet — sign in to the dashboard, or paste one below.",
        reply.hasSession
      );
    } else {
      setSessionStatus("Could not read settings: " + (reply.error || "unknown"), false);
    }
  }

  async function save() {
    const settings = {
      agentUrl: fields.agentUrl.value.trim() || VapApi.DEFAULT_SETTINGS.agentUrl,
      panelHost: fields.panelHost.value.trim(),
    };
    await chrome.storage.sync.set(settings);
    const token = fields.sessionToken.value.trim();
    if (token) {
      await chrome.storage.local.set({ sessionToken: token });
      fields.sessionToken.value = "";
      setSessionStatus("Session token: saved ✓", true);
    }
    const note = $("#save-note");
    note.hidden = false;
    setTimeout(() => (note.hidden = true), 2000);
    await rpc({ type: "get-settings" });
  }

  async function detect() {
    setSessionStatus("Looking for a session cookie…", false);
    const reply = await rpc({ type: "detect-session" });
    if (reply.ok) {
      setSessionStatus(`Session token: captured ✓ (source: ${reply.source})`, true);
    } else {
      setSessionStatus(reply.error || "no session found — paste a token instead", false);
    }
  }

  async function health() {
    const out = $("#diagnostics");
    out.textContent = "Testing…";
    const reply = await rpc({
      type: "healthz",
      agentUrl: fields.agentUrl.value.trim() || undefined,
    });
    if (reply.ok) {
      try {
        const body = JSON.parse(reply.body);
        out.textContent =
          `agent reachable (HTTP ${reply.status})\n` +
          `status:          ${body.status}\n` +
          `llm configured:  ${body.llm_configured}\n` +
          `version:         ${body.version}`;
      } catch (_err) {
        out.textContent = `HTTP ${reply.status}\n${reply.body}`;
      }
    } else {
      out.textContent =
        `cannot reach the agent: ${reply.error || reply.body || `HTTP ${reply.status}`}\n` +
        `check the agent URL (current: ${fields.agentUrl.value || VapApi.DEFAULT_SETTINGS.agentUrl})`;
    }
  }

  async function reset() {
    await chrome.storage.sync.set(VapApi.DEFAULT_SETTINGS);
    await chrome.storage.local.remove("sessionToken");
    fields.sessionToken.value = "";
    await load();
  }

  $("#btn-save").addEventListener("click", save);
  $("#btn-detect").addEventListener("click", detect);
  $("#btn-health").addEventListener("click", health);
  $("#btn-reset").addEventListener("click", reset);

  load();
})();
