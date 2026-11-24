/* Virtuozzo Application Management Assistant options page logic:
 * one visible field (Platform, auto-detected), everything else in Advanced. */
"use strict";

(() => {
  const $ = (sel) => document.querySelector(sel);
  const fields = {
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
      fields.panelHost.value = reply.settings.panelHost || "";
      const hint = $("#platform-hint");
      if (reply.settings.panelHost) {
        hint.textContent = `Pinned to ${reply.settings.panelHost}. Clear the field to go back to auto-detection.`;
      } else {
        hint.textContent = "Detected automatically when you open the dashboard. Type a URL here only to pin a specific platform.";
      }
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
    await chrome.storage.sync.set({ panelHost: fields.panelHost.value.trim() });
    const token = fields.sessionToken.value.trim();
    if (token) {
      await chrome.storage.local.set({ sessionToken: token });
      fields.sessionToken.value = "";
      setSessionStatus("Session token: saved ✓", true);
    }
    const note = $("#save-note");
    note.hidden = false;
    setTimeout(() => (note.hidden = true), 2000);
    await load();
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
    const reply = await rpc({ type: "healthz" });
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
        `cannot reach the assistant service: ${reply.error || reply.body || `HTTP ${reply.status}`}\n` +
        `the service may be offline — try again in a moment`;
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
