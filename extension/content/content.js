/* Virtuozzo Application Management Assistant — content script: FAB "AI Assistant" + env context capture.
 *
 * Runs on any http(s) page but only renders on detected dashboard pages:
 *   - <meta name="vap-dashboard"> (mock dashboard / any host page opt-in)
 *   - window.jelastic global (Jelastic dashboard marker)
 *   - <body data-jelastic> or title containing "Jelastic"
 *   - hostname matches the configured panel host
 *
 * The FAB lives in an open shadow root so page CSS cannot break it.
 */
"use strict";

(() => {
  const HOST_ID = "vap-copilot-fab-root";
  if (document.getElementById(HOST_ID)) return;

  /* ---------------- detection ---------------- */

  function isDashboard() {
    if (document.querySelector('meta[name="vap-dashboard"]')) return true;
    if (document.querySelector("[data-jelastic], #jelastic-dashboard")) return true;
    if (typeof window.jelastic !== "undefined") return true;
    const title = (document.title || "") + " " + (document.body && document.body.className || "");
    if (/jelastic|virtuozzo application manager/i.test(title)) return true;
    return false;
  }

  function isPanelHost() {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage({ type: "get-settings" }, (reply) => {
          const host = reply && reply.settings && reply.settings.panelHost;
          resolve(Boolean(host) && location.href.startsWith(host));
        });
      } catch (_err) {
        resolve(false);
      }
    });
  }

  /* ---------------- context capture ---------------- */

  /** Sample the dashboard's ACTUALLY PAINTED background so the side panel can
   *  continue it seamlessly (DESIGN.md: the panel must not differ from the
   *  dashboard background). Many dashboards (Jelastic/VHC included) paint the
   *  background on a wrapper <div>, not on body/html — so walk the element
   *  stack at several viewport points and take the dominant non-transparent
   *  color; fall back to <meta name="theme-color">. Returns
   *  {color, image, light}. */
  function sampleTheme() {
    try {
      const points = [
        [window.innerWidth * 0.5, window.innerHeight * 0.08],
        [window.innerWidth * 0.5, window.innerHeight * 0.5],
        [window.innerWidth * 0.08, window.innerHeight * 0.85],
        [window.innerWidth * 0.9, window.innerHeight * 0.85],
      ];
      const counts = new Map();
      let image = null;
      for (const [x, y] of points) {
        let stack = [];
        if (document.elementsFromPoint) {
          stack = document.elementsFromPoint(x, y);
        }
        stack = stack.concat([document.body, document.documentElement]);
        /* The page background is the TOPMOST FULL-BLEED painted layer:
         * cards/headers are painted too but never cover >=80% of the
         * viewport, so they are skipped; wrapper-div backgrounds (the
         * Jelastic/VHC case) are found because they do cover it. */
        for (const el of stack) {
          if (!el || el.nodeType !== 1) continue;
          const cs = getComputedStyle(el);
          const bg = cs.backgroundColor;
          if (!bg || /rgba\(0,\s*0,\s*0,\s*0\)|transparent/.test(bg)) continue;
          const rect = el.getBoundingClientRect();
          const fullBleed =
            rect.width >= window.innerWidth * 0.8 &&
            rect.height >= window.innerHeight * 0.8;
          if (!fullBleed) continue;
          counts.set(bg, (counts.get(bg) || 0) + 1);
          if (!image && cs.backgroundImage && cs.backgroundImage !== "none") {
            image = cs.backgroundImage;
          }
          break;
        }
      }
      let color = null;
      let best = 0;
      for (const [c, n] of counts) {
        if (n > best) { best = n; color = c; }
      }
      if (!color) {
        const meta = document.querySelector('meta[name="theme-color"]');
        if (meta && meta.content) color = meta.content;
      }
      const m = String(color || "").match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
      let light = false;
      if (m) {
        const chan = [Number(m[1]), Number(m[2]), Number(m[3])].map((v) => {
          const s = v / 255;
          return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
        });
        light = 0.2126 * chan[0] + 0.7152 * chan[1] + 0.0722 * chan[2] > 0.4;
      }
      return color ? { color, image, light } : null;
    } catch (_err) {
      return null;
    }
  }

  function captureContext() {
    const url = new URL(location.href);
    let envName =
      url.searchParams.get("env") ||
      url.searchParams.get("envName") ||
      (location.pathname.match(/\/env\/([^/]+)/) || [])[1] ||
      null;

    const domEnv = document.querySelector(
      "[data-env-name], .env-name, #env-name, .environment-name"
    );
    if (!envName && domEnv) envName = (domEnv.textContent || "").trim() || null;

    let page = "dashboard";
    if (/marketplace/i.test(location.href)) page = "marketplace";
    else if (envName) page = "env";

    return { envName, page, url: location.href, theme: sampleTheme() };
  }

  function scanPageToken() {
    /* Jelastic keeps the session in page storage; best-effort scan. */
    try {
      for (const storage of [window.sessionStorage, window.localStorage]) {
        for (let i = 0; i < storage.length; i++) {
          const key = storage.key(i);
          if (/session|token|jelastic/i.test(key)) {
            const value = storage.getItem(key);
            if (value && value.length > 8 && !/[{}]/.test(value.slice(0, 2))) return value;
          }
        }
      }
    } catch (_err) {
      /* cross-origin storage access denied — fine */
    }
    return null;
  }

  /* ---------------- FAB ---------------- */

  const ICON_SPARKLE = `
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M12 2l2.2 6.1L20 10.3l-5.8 2.2L12 18.6l-2.2-6.1L4 10.3l5.8-2.2L12 2z"
            fill="white"/>
      <circle cx="19.2" cy="18.8" r="1.7" fill="white" opacity="0.85"/>
      <circle cx="5.2" cy="18.2" r="1.1" fill="white" opacity="0.7"/>
    </svg>`;

  function injectFab() {
    const host = document.createElement("div");
    host.id = HOST_ID;
    const shadow = host.attachShadow({ mode: "open" });

    const style = document.createElement("style");
    style.textContent = `
      :host { all: initial; }
      .fab {
        position: fixed;
        right: 20px;
        bottom: 20px;
        z-index: 2147483646;
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 11px 18px 11px 14px;
        border: 1px solid rgba(255, 255, 255, 0.16);
        border-radius: 999px;
        background: linear-gradient(135deg, #234F95, #3B82F6);
        color: #fff;
        font: 600 13.5px/1 Inter, system-ui, -apple-system, sans-serif;
        letter-spacing: 0.01em;
        cursor: pointer;
        box-shadow: 0 8px 28px rgba(11, 18, 32, 0.45), 0 2px 8px rgba(35, 79, 149, 0.4);
        transition: transform 180ms ease-out, box-shadow 180ms ease-out, filter 180ms ease-out;
        user-select: none;
      }
      .fab:hover {
        transform: translateY(-2px);
        filter: brightness(1.08);
        box-shadow: 0 12px 32px rgba(11, 18, 32, 0.5), 0 4px 12px rgba(35, 79, 149, 0.5);
      }
      .fab:active { transform: translateY(0) scale(0.98); }
      .fab .dot {
        width: 8px; height: 8px; border-radius: 50%;
        background: #7FFFA8;
        box-shadow: 0 0 8px rgba(127, 255, 168, 0.9);
      }
      @media (prefers-reduced-motion: reduce) {
        .fab { transition: none; }
      }
    `;

    const button = document.createElement("button");
    button.className = "fab";
    button.id = "vap-fab";
    button.setAttribute("aria-label", "Open AI Assistant");
    button.innerHTML = `${ICON_SPARKLE}<span>AI Assistant</span><span class="dot" title="connected"></span>`;
    button.addEventListener("click", () => {
      const context = captureContext();
      button.disabled = true;
      try {
        chrome.runtime.sendMessage(
          { type: "open-sidepanel", context },
          (reply) => {
            /* debug hook: how the panel was opened (sidePanel | tab) */
            host.setAttribute("data-opened",
              (reply && reply.via) || (reply && reply.ok ? "sidePanel" : "failed"));
            setTimeout(() => (button.disabled = false), 800);
          }
        );
      } catch (_err) {
        button.disabled = false;
      }
    });

    shadow.appendChild(style);
    shadow.appendChild(button);
    document.documentElement.appendChild(host);
    return host;
  }

  /* ---------------- boot ---------------- */

  async function boot() {
    const dashboard = isDashboard();
    const onPanelHost = await isPanelHost();
    if (!dashboard && !onPanelHost) return;

    injectFab();

    const context = captureContext();
    try {
      chrome.runtime.sendMessage({ type: "page-context", context });
    } catch (_err) {
      /* extension reloading */
    }

    const token = scanPageToken();
    if (token) {
      try {
        chrome.runtime.sendMessage({ type: "session-token", token });
      } catch (_err) {
        /* ignore */
      }
    }
  }

  boot();
})();
