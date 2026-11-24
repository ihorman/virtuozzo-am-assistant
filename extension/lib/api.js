/* Shared helpers for the VAP Copilot extension (background + sidepanel).
 *
 * Plain classic script: loaded via importScripts() in the service worker and
 * via <script src> in the sidepanel. Everything hangs off the global
 * `VapApi` object so page scripts can never collide with it.
 */
"use strict";

var VapApi = (() => {

  /** Defaults — "no configuration needed to start" (DESIGN.md UX rules). */
  const DEFAULT_SETTINGS = {
    /* Hosted default agent endpoint (PLAN §7). */
    agentUrl: "https://hermes.bannoe.it/am-assistant",
    /* Empty = auto: the FAB appears on any detected dashboard page. */
    panelHost: "",
  };

  /* ------------------------------------------------------------------
   * SSE parsing (incremental; agent streams `event:` + `data:` messages,
   * keepalives are `: comment` lines — docs/API-CONTRACT.md).
   * ------------------------------------------------------------------ */
  class SSEParser {
    constructor() {
      this._buffer = "";
    }

    /** Feed raw text; returns a list of {event, data} decoded events. */
    feed(text) {
      this._buffer += text.replace(/\r\n/g, "\n");
      const events = [];
      let index;
      while ((index = this._buffer.indexOf("\n\n")) !== -1) {
        const raw = this._buffer.slice(0, index);
        this._buffer = this._buffer.slice(index + 2);
        const parsed = this._parseBlock(raw);
        if (parsed) events.push(parsed);
      }
      return events;
    }

    _parseBlock(raw) {
      let name = "message";
      const dataLines = [];
      for (const line of raw.split("\n")) {
        if (!line || line.startsWith(":")) continue;      // comment/keepalive
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
      }
      if (!dataLines.length) return null;
      let data;
      try {
        data = JSON.parse(dataLines.join("\n"));
      } catch (_err) {
        data = { text: dataLines.join("\n") };
      }
      return { event: name, data };
    }
  }

  /* ------------------------------------------------------------------
   * Error-code translation (never show raw codes — DESIGN.md item 9).
   * ------------------------------------------------------------------ */
  const ERROR_COPY = {
    session_expired: {
      title: "Your platform session expired",
      body: "Open the dashboard again (or paste a fresh session token in Settings) and retry.",
      fix: "open_settings",
    },
    no_credentials: {
      title: "Not connected to the platform",
      body: "Sign in to your dashboard, or paste a session token in Settings.",
      fix: "open_settings",
    },
    platform_error: {
      title: "The platform reported a problem",
      body: "This is usually temporary. Retry, or let me diagnose it.",
      fix: "retry",
    },
    llm_not_configured: {
      title: "The assistant brain is not configured",
      body: "The agent has no LLM endpoint set. This is a server-side setting.",
      fix: null,
    },
    bad_request: {
      title: "Something went wrong with that request",
      body: "Try rephrasing — if it keeps happening, the agent and extension versions may not match.",
      fix: "retry",
    },
    network_error: {
      title: "Cannot reach the assistant",
      body: "The agent server is unreachable. Check the agent URL in Settings.",
      fix: "open_settings",
    },
  };

  function errorCopy(code) {
    return ERROR_COPY[code] || {
      title: "Something went wrong",
      body: code ? `Unexpected problem (${code}). Try again.` : "Try again.",
      fix: "retry",
    };
  }

  /* ------------------------------------------------------------------
   * Tiny markdown renderer (safe: everything is escaped first).
   * Supports: fenced code (+ copy button hook), inline code, bold,
   * italic, links (http/https only), headings, lists, paragraphs.
   * ------------------------------------------------------------------ */
  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function inlineMd(text) {
    let out = escapeHtml(text);
    out = out.replace(/`([^`]+)`/g, (_m, code) => `<code>${code}</code>`);
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[\s(])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    out = out.replace(
      /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      (_m, label, url) => `<a href="${url}" target="_blank" rel="noopener">${label}</a>`
    );
    return out;
  }

  function renderMarkdown(md) {
    const lines = String(md || "").split("\n");
    const html = [];
    let inCode = false;
    let codeLines = [];
    let listOpen = false;

    const closeList = () => {
      if (listOpen) {
        html.push("</ul>");
        listOpen = false;
      }
    };

    for (const line of lines) {
      if (line.trim().startsWith("```")) {
        if (inCode) {
          html.push(
            `<div class="md-code"><button class="md-copy" data-copy="${escapeHtml(
              codeLines.join("\n")
            )}">Copy</button><pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre></div>`
          );
          codeLines = [];
          inCode = false;
        } else {
          closeList();
          inCode = true;
        }
        continue;
      }
      if (inCode) {
        codeLines.push(line);
        continue;
      }
      const heading = line.match(/^(#{1,3})\s+(.*)$/);
      if (heading) {
        closeList();
        const level = heading[1].length + 1; // h2..h4 inside cards
        html.push(`<h${level}>${inlineMd(heading[2])}</h${level}>`);
        continue;
      }
      const bullet = line.match(/^\s*[-*]\s+(.*)$/);
      if (bullet) {
        if (!listOpen) {
          html.push("<ul>");
          listOpen = true;
        }
        html.push(`<li>${inlineMd(bullet[1])}</li>`);
        continue;
      }
      if (!line.trim()) {
        closeList();
        continue;
      }
      closeList();
      html.push(`<p>${inlineMd(line)}</p>`);
    }
    if (inCode) {
      html.push(
        `<div class="md-code"><button class="md-copy" data-copy="${escapeHtml(
          codeLines.join("\n")
        )}">Copy</button><pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre></div>`
      );
    }
    closeList();
    return html.join("\n");
  }

  /** mono values the user may need later -> copy buttons (DESIGN.md UX). */
  function renderMonoValues(values) {
    return values
      .map(
        (v) =>
          `<div class="mono-value"><code>${escapeHtml(v)}</code>` +
          `<button class="md-copy" data-copy="${escapeHtml(v)}">Copy</button></div>`
      )
      .join("");
  }

  return {
    DEFAULT_SETTINGS,
    SSEParser,
    errorCopy,
    escapeHtml,
    renderMarkdown,
    renderMonoValues,
    inlineMd,
  };
})();
