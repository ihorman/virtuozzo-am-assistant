/* Application Management Assistant — background service worker.
 *
 * Responsibilities (docs/PLAN.md §4):
 *   - settings (chrome.storage.sync) + session token (chrome.storage.local)
 *   - session capture (cookies + page-scanned tokens) for the platform host
 *   - fetch proxy: the sidepanel streams SSE through here so auth headers
 *     and the session token live in one place (never in page JS)
 *   - side panel opening (toolbar action + FAB message)
 */
"use strict";

importScripts("lib/api.js");

/* ------------------------------------------------------------------ */
/* Settings                                                            */
/* ------------------------------------------------------------------ */

async function getSettings() {
  const stored = await chrome.storage.sync.get(Object.keys(VapApi.DEFAULT_SETTINGS));
  return { ...VapApi.DEFAULT_SETTINGS, ...stored };
}

async function getSessionToken() {
  const { sessionToken } = await chrome.storage.local.get("sessionToken");
  return sessionToken || null;
}

/** Best-effort session capture from the platform host's cookies. */
async function captureSessionFromCookies(panelHost) {
  if (!panelHost || !/^https?:\/\//.test(panelHost)) return null;
  let host;
  try {
    host = new URL(panelHost).hostname;
  } catch (_err) {
    return null;
  }
  const cookies = await chrome.cookies.getAll({ domain: host });
  const preferred = cookies.find((c) =>
    /session|token|hs\b|jelastic/i.test(c.name)
  ) || cookies.find((c) => c.value && c.value.length > 20);
  return preferred ? preferred.value : null;
}

/* ------------------------------------------------------------------ */
/* Side panel behavior                                                 */
/* ------------------------------------------------------------------ */

chrome.runtime.onInstalled.addListener(async () => {
  try {
    await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  } catch (_err) {
    /* older Chromium: action click falls back to message handler below */
  }
});

chrome.runtime.onStartup.addListener(async () => {
  try {
    await chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });
  } catch (_err) {
    /* ignore */
  }
});

async function openSidePanel(tabId, windowId) {
  try {
    if (chrome.sidePanel && chrome.sidePanel.open) {
      if (tabId != null) await chrome.sidePanel.open({ tabId });
      else if (windowId != null) await chrome.sidePanel.open({ windowId });
      return { opened: true, via: "sidePanel" };
    }
  } catch (_err) {
    /* fall through */
  }
  /* Fallback: open the sidepanel page as a regular tab (headless test
   * path + very old Chrome). */
  await chrome.tabs.create({ url: chrome.runtime.getURL("sidepanel/sidepanel.html") });
  return { opened: true, via: "tab" };
}

chrome.action.onClicked.addListener(async (tab) => {
  if (tab && tab.id != null) await openSidePanel(tab.id, tab.windowId);
});

/* ------------------------------------------------------------------ */
/* Messages from the content script / sidepanel                        */
/* ------------------------------------------------------------------ */

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      const reply = await handleMessage(message || {}, sender);
      sendResponse(reply);
    } catch (err) {
      sendResponse({ ok: false, error: String(err && err.message ? err.message : err) });
    }
  })();
  return true; /* async sendResponse */
});

async function handleMessage(message, sender) {
  switch (message.type) {
    case "open-sidepanel": {
      const tabId = sender.tab && sender.tab.id;
      const windowId = sender.tab && sender.tab.windowId;
      if (message.context) {
        await chrome.storage.local.set({ lastContext: message.context });
      }
      return openSidePanel(tabId, windowId);
    }
    case "page-context": {
      if (message.context) {
        await chrome.storage.local.set({ lastContext: message.context });
      }
      return { ok: true };
    }
    case "session-token": {
      /* token scanned from the dashboard page (Jelastic keeps it in the
       * page storage) — only accept it from a page on the panel host. */
      const settings = await getSettings();
      const senderUrl = (sender.url || (sender.tab && sender.tab.url) || "");
      const hostOk = !settings.panelHost || senderUrl.startsWith(settings.panelHost);
      if (!hostOk) return { ok: false, error: "sender host mismatch" };
      if (!message.token) return { ok: false, error: "no token" };
      await chrome.storage.local.set({ sessionToken: message.token });
      return { ok: true };
    }
    case "get-context": {
      const { lastContext } = await chrome.storage.local.get("lastContext");
      return { ok: true, context: lastContext || null };
    }
    case "get-settings": {
      const settings = await getSettings();
      return { ok: true, settings, hasSession: Boolean(await getSessionToken()) };
    }
    case "healthz": {
      const settings = await getSettings();
      const url = String(message.agentUrl || settings.agentUrl).replace(/\/+$/, "");
      const response = await fetch(`${url}/healthz`, {
        method: "GET",
        headers: await authHeaders(settings),
      });
      return { ok: response.ok, status: response.status, body: await response.text() };
    }
    case "detect-session": {
      const settings = await getSettings();
      const token = await captureSessionFromCookies(settings.panelHost);
      if (token) {
        await chrome.storage.local.set({ sessionToken: token });
        return { ok: true, source: "cookies" };
      }
      return { ok: false, error: "no session cookie found — paste a token instead" };
    }
    default:
      return { ok: false, error: `unknown message [${message.type}]` };
  }
}

/* ------------------------------------------------------------------ */
/* Fetch proxy (streaming) — port "fetch-proxy"                        */
/*                                                                     */
/* Requests:  {id, url, method, headers, body}                         */
/* Responses: {id, type:"chunk", text} | {id, type:"end", status}       */
/*            | {id, type:"error", message}                            */
/*                                                                     */
/* The URL must share the origin of the configured agent URL — this is */
/* a proxy to the agent only, never an open proxy.                     */
/* ------------------------------------------------------------------ */

async function authHeaders(settings, extra = {}) {
  const headers = { ...extra };
  const token = await getSessionToken();
  if (settings.panelHost) headers["X-Jelastic-Host"] = settings.panelHost;
  if (token) headers["X-Jelastic-Session"] = token;
  const { lastContext } = await chrome.storage.local.get("lastContext");
  if (lastContext) {
    /* theme is display-only — keep the wire contract {envName, page, url}. */
    const { theme, ...ctx } = lastContext;
    headers["X-Vap-Context"] = JSON.stringify(ctx);
  }
  return headers;
}

function agentOrigin(settings) {
  try {
    return new URL(settings.agentUrl).origin;
  } catch (_err) {
    return null;
  }
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "fetch-proxy") return;
  port.onMessage.addListener(async (request) => {
    const id = request && request.id;
    try {
      const settings = await getSettings();
      const target = new URL(request.url);
      if (target.origin !== agentOrigin(settings)) {
        port.postMessage({
          id,
          type: "error",
          message: `refusing to proxy to [${target.origin}] (agent is [${agentOrigin(settings)}])`,
        });
        return;
      }
      const headers = await authHeaders(settings, request.headers || {});
      const response = await fetch(target.href, {
        method: request.method || "GET",
        headers,
        body: request.body || undefined,
      });
      /* response head first (status + headers: X-Conversation-Id, …) */
      const responseHeaders = {};
      response.headers.forEach((value, key) => {
        responseHeaders[key] = value;
      });
      port.postMessage({
        id,
        type: "headers",
        status: response.status,
        headers: responseHeaders,
      });
      if (!response.body) {
        const text = await response.text();
        port.postMessage({ id, type: "chunk", text });
        port.postMessage({ id, type: "end", status: response.status });
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        port.postMessage({ id, type: "chunk", text: decoder.decode(value, { stream: true }) });
      }
      port.postMessage({ id, type: "end", status: response.status });
    } catch (err) {
      port.postMessage({
        id,
        type: "error",
        message: String(err && err.message ? err.message : err),
      });
    }
  });
});
