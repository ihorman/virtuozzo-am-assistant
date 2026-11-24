/* VAP Copilot — sidepanel chat UI.
 *
 * Streams one turn per user message from the agent (`POST /api/chat`, SSE),
 * renders every contract event (docs/API-CONTRACT.md v1.1/1.2) as a designed
 * component (docs/DESIGN.md items 1-11), and resolves ask/confirm/card
 * actions through `POST /api/answer`.
 *
 * All network traffic goes through the background fetch proxy, which owns
 * the session token and auth headers.
 */
"use strict";

(() => {
  const $ = (sel) => document.querySelector(sel);

  const el = {
    messages: $("#messages"),
    empty: $("#empty-state"),
    input: $("#input"),
    send: $("#send"),
    envPill: $("#env-pill"),
    envName: $("#env-name"),
    envStatus: $("#env-status"),
    hostLabel: $("#host-label"),
    banner: $("#status-banner"),
  };

  const state = {
    agentUrl: VapApi.DEFAULT_SETTINGS.agentUrl,
    conversationId: null,
    busy: false,
    askCount: 0,         // wizard step tracking within a turn
  };

  /* ---------------------------------------------------------------- */
  /* settings / context                                                */
  /* ---------------------------------------------------------------- */

  function rpc(message) {
    return new Promise((resolve) => {
      try {
        chrome.runtime.sendMessage(message, (reply) => resolve(reply || { ok: false }));
      } catch (err) {
        resolve({ ok: false, error: String(err) });
      }
    });
  }

  async function init() {
    const reply = await rpc({ type: "get-settings" });
    if (reply.ok && reply.settings) {
      state.agentUrl = String(reply.settings.agentUrl || state.agentUrl).replace(/\/+$/, "");
      try {
        el.hostLabel.textContent = new URL(state.agentUrl).host;
      } catch (_err) {
        el.hostLabel.textContent = "AI Assistant";
      }
    }
    const ctxReply = await rpc({ type: "get-context" });
    if (ctxReply.ok && ctxReply.context) applyContext(ctxReply.context);

    chrome.runtime.onMessage.addListener((message) => {
      if (message && message.type === "page-context" && message.context) {
        applyContext(message.context);
      }
    });
  }

  function applyContext(context) {
    if (context.envName) {
      el.envPill.hidden = false;
      el.envName.textContent = context.envName;
      const status = context.status || "running";
      el.envStatus.className = "status-dot" +
        (status === "running" ? "" : status === "sleeping" ? " warn" : " muted");
      el.envStatus.title = status;
    }
    if (context.url) {
      try {
        el.hostLabel.textContent = new URL(context.url).host;
      } catch (_err) {
        /* keep */
      }
    }
  }

  /* ---------------------------------------------------------------- */
  /* composer                                                          */
  /* ---------------------------------------------------------------- */

  function setBusy(busy) {
    state.busy = busy;
    el.send.disabled = busy || !el.input.value.trim();
  }

  function autoGrow() {
    el.input.style.height = "auto";
    el.input.style.height = Math.min(el.input.scrollHeight, 120) + "px";
  }

  el.input.addEventListener("input", () => {
    autoGrow();
    el.send.disabled = state.busy || !el.input.value.trim();
  });

  el.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendFromComposer();
    }
  });

  el.send.addEventListener("click", sendFromComposer);

  function sendFromComposer() {
    const text = el.input.value.trim();
    if (!text || state.busy) return;
    el.input.value = "";
    autoGrow();
    sendMessage(text);
  }

  $("#btn-settings").addEventListener("click", () => {
    try {
      chrome.runtime.openOptionsPage();
    } catch (_err) {
      window.open(chrome.runtime.getURL("options/options.html"));
    }
  });

  $("#btn-audit").addEventListener("click", showAudit);

  $("#suggestions").addEventListener("click", (event) => {
    const chip = event.target.closest(".chip.suggestion");
    if (!chip || state.busy) return;
    sendMessage(chip.textContent.trim());
  });

  /* copy buttons (delegated) */
  document.addEventListener("click", (event) => {
    const copy = event.target.closest(".md-copy");
    if (!copy) return;
    const value = copy.getAttribute("data-copy") || "";
    navigator.clipboard.writeText(value).then(() => {
      copy.classList.add("copied");
      copy.textContent = "Copied";
      setTimeout(() => {
        copy.classList.remove("copied");
        copy.textContent = "Copy";
      }, 1500);
    });
  });

  /* ---------------------------------------------------------------- */
  /* turn streaming (fetch proxy + SSE parser)                         */
  /* ---------------------------------------------------------------- */

  function hideEmpty() {
    if (el.empty && !el.empty.hidden) el.empty.hidden = true;
  }

  function scrollDown() {
    el.messages.scrollTop = el.messages.scrollHeight;
  }

  function addUserMessage(text) {
    hideEmpty();
    const bubble = document.createElement("div");
    bubble.className = "msg-user";
    bubble.textContent = text;
    el.messages.appendChild(bubble);
    scrollDown();
  }

  function beginTurn() {
    hideEmpty();
    const turn = document.createElement("div");
    turn.className = "turn";
    turn.style.display = "flex";
    turn.style.flexDirection = "column";
    turn.style.gap = "10px";
    el.messages.appendChild(turn);
    state.askCount = 0;
    return turn;
  }

  function sendMessage(text) {
    addUserMessage(text);
    const turn = beginTurn();
    setBusy(true);

    const ctx = {
      turn,
      card: null,          // assistant text bubble (lazily created)
      text: "",            // accumulated narration
      typing: null,        // typing-indicator bubble while waiting
    };

    const ensureCard = () => {
      if (ctx.card) return ctx.card;
      if (ctx.typing) {
        ctx.typing.remove();
        ctx.typing = null;
      }
      const card = document.createElement("div");
      card.className = "msg-assistant";
      card.innerHTML = `<div class="md"></div>`;
      turn.appendChild(card);
      ctx.card = card;
      return card;
    };

    const showText = () => {
      ensureCard().querySelector(".md").innerHTML = VapApi.renderMarkdown(ctx.text);
      scrollDown();
    };

    /* typing indicator until the first token arrives */
    const typing = document.createElement("div");
    typing.className = "msg-assistant";
    typing.innerHTML = `<div class="typing"><span></span><span></span><span></span></div>`;
    turn.appendChild(typing);
    ctx.typing = typing;

    const parser = new VapApi.SSEParser();
    const port = chrome.runtime.connect({ name: "fetch-proxy" });
    const requestId = "chat-" + Date.now();

    const finish = () => {
      if (ctx.typing) {
        ctx.typing.remove();
        ctx.typing = null;
      }
      settleProgress(turn);
      setBusy(false);
      el.input.focus();
    };

    port.onMessage.addListener((message) => {
      if (!message || message.id !== requestId) return;
      if (message.type === "headers") {
        const convId = message.headers && message.headers["x-conversation-id"];
        if (convId && !state.conversationId) state.conversationId = convId;
      } else if (message.type === "chunk") {
        for (const event of parser.feed(message.text)) {
          handleEvent(event, ctx, showText);
        }
      } else if (message.type === "end") {
        try { port.disconnect(); } catch (_err) { /* ignore */ }
        finish();
      } else if (message.type === "error") {
        try { port.disconnect(); } catch (_err) { /* ignore */ }
        if (ctx.typing) {
          ctx.typing.remove();
          ctx.typing = null;
        }
        renderErrorCard(turn, "network_error", message.message);
        setBusy(false);
      }
    });

    port.postMessage({
      id: requestId,
      url: state.agentUrl + "/api/chat",
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, conversation_id: state.conversationId }),
    });
  }

  /* ---------------------------------------------------------------- */
  /* event handlers (docs/API-CONTRACT.md)                             */
  /* ---------------------------------------------------------------- */

  function handleEvent(event, ctx, showText) {
    const turn = ctx.turn;
    switch (event.event) {
      case "token":
        ctx.text += (event.data.text || "");
        showText();
        break;
      case "tool_call":
        addToolRow(turn, event.data);
        break;
      case "tool_result":
        resolveToolRow(turn, event.data);
        break;
      case "ask_user":
        renderAskCard(turn, event.data);
        break;
      case "confirm_action":
        renderConfirmCard(turn, event.data);
        break;
      case "progress":
        renderProgressCard(turn, event.data);
        break;
      case "card":
        renderResultCard(turn, event.data);
        break;
      case "done":
        state.conversationId = event.data.conversation_id || state.conversationId;
        settleProgress(turn);
        break;
      case "error":
        renderErrorCard(turn, event.data.code, event.data.message);
        break;
      default:
        break;
    }
    scrollDown();
  }

  /* ---------------------------------------------------------------- */
  /* tool rows                                                         */
  /* ---------------------------------------------------------------- */

  function addToolRow(turn, data) {
    const row = document.createElement("div");
    row.className = "tool-row";
    row.dataset.callId = data.call_id || "";
    row.dataset.toolName = data.name || "tool";
    row.innerHTML =
      `<span class="typing"><span></span><span></span><span></span></span>` +
      `<span>Running</span><span class="tool-name">${VapApi.escapeHtml(data.name || "tool")}</span>`;
    turn.appendChild(row);
  }

  function resolveToolRow(turn, data) {
    const row = turn.querySelector(`.tool-row[data-call-id="${CSS.escape(data.call_id || "")}"]`);
    if (!row) return;
    const name = row.dataset.toolName || "";
    row.classList.add(data.ok ? "ok" : "fail");
    row.innerHTML =
      `<span class="pill ${data.ok ? "ok" : "high"}">${data.ok ? "done" : "failed"}</span>` +
      `<span class="tool-name">${VapApi.escapeHtml(name)}</span>` +
      (data.summary ? `<span>${VapApi.escapeHtml(data.summary)}</span>` : "");
  }

  /* ---------------------------------------------------------------- */
  /* wizard step dots (DESIGN.md item 4)                               */
  /* ---------------------------------------------------------------- */

  function stepDots() {
    state.askCount += 1;
    const step = state.askCount;
    const wrap = document.createElement("div");
    wrap.className = "steps";
    const total = Math.min(Math.max(step, 2), 5);
    for (let i = 1; i <= total; i++) {
      const dot = document.createElement("span");
      dot.className = "step " + (i < step ? "done" : i === step ? "current" : "");
      wrap.appendChild(dot);
    }
    const label = document.createElement("span");
    label.className = "step-label";
    label.textContent = `Step ${step}`;
    wrap.appendChild(label);
    return wrap;
  }

  /* ---------------------------------------------------------------- */
  /* ask_user card (DESIGN.md item 5)                                  */
  /* ---------------------------------------------------------------- */

  function renderAskCard(turn, data) {
    const card = document.createElement("div");
    card.className = "card ask";
    card.appendChild(stepDots());

    const question = document.createElement("div");
    question.className = "ask-question";
    question.textContent = data.question || "What would you like?";
    card.appendChild(question);

    const options = document.createElement("div");
    options.className = "ask-options";

    let freeInput = null;
    let freeSend = null;
    const answer = (value) => {
      if (card.dataset.answered) return;
      card.dataset.answered = value;
      options.querySelectorAll("button").forEach((b) => (b.disabled = true));
      if (freeInput) freeInput.disabled = true;
      if (freeSend) freeSend.disabled = true;
      answerRequest({ conversation_id: state.conversationId, ask_id: data.ask_id, answer: value });
      const note = document.createElement("div");
      note.className = "muted";
      note.style.marginTop = "8px";
      note.textContent = `You answered: ${value}`;
      card.appendChild(note);
    };

    for (const option of data.options || []) {
      const chip = document.createElement("button");
      chip.className = "chip";
      chip.textContent = option;
      chip.addEventListener("click", () => answer(option));
      options.appendChild(chip);
    }
    card.appendChild(options);

    if (data.allow_free !== false) {
      const free = document.createElement("div");
      free.className = "free-text";
      freeInput = document.createElement("input");
      freeInput.placeholder = "Or type your own answer…";
      freeSend = document.createElement("button");
      freeSend.className = "btn";
      freeSend.textContent = "Send";
      const doFree = () => {
        const value = freeInput.value.trim();
        if (value) answer(value);
      };
      freeSend.addEventListener("click", doFree);
      freeInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          doFree();
        }
      });
      free.appendChild(freeInput);
      free.appendChild(freeSend);
      card.appendChild(free);
    }

    turn.appendChild(card);
    scrollDown();
  }

  /* ---------------------------------------------------------------- */
  /* confirm_action card (DESIGN.md item 6)                            */
  /* ---------------------------------------------------------------- */

  function renderConfirmCard(turn, data) {
    const card = document.createElement("div");
    card.className = "card confirm" + (data.danger ? " danger" : "");

    const title = document.createElement("div");
    title.className = "card-title";
    if (data.danger) {
      title.innerHTML = `<span class="pill high">destructive</span>`;
    }
    title.appendChild(document.createTextNode(data.summary ? "Ready to run" : "Confirm action"));
    card.appendChild(title);

    const recap = document.createElement("div");
    recap.className = "confirm-recap";
    recap.textContent = data.summary || "";
    card.appendChild(recap);

    if (Array.isArray(data.actions) && data.actions.length) {
      const list = document.createElement("ul");
      list.className = "confirm-actions-list";
      for (const action of data.actions) {
        const li = document.createElement("li");
        li.textContent = action;
        list.appendChild(li);
      }
      card.appendChild(list);
    }

    const decide = (approved) => {
      if (card.dataset.answered) return;
      card.dataset.answered = String(approved);
      card.querySelectorAll("button, input").forEach((b) => (b.disabled = true));
      answerRequest({
        conversation_id: state.conversationId,
        confirm_id: data.confirm_id,
        approved,
      });
      const note = document.createElement("div");
      note.className = "muted";
      note.style.marginTop = "8px";
      note.textContent = approved ? "Confirmed — running." : "Cancelled.";
      card.appendChild(note);
    };

    if (data.danger) {
      const typed = document.createElement("div");
      typed.className = "typed-confirm";
      typed.innerHTML = `Type <code>confirm</code> to proceed:`;
      const row = document.createElement("div");
      row.className = "row";
      const input = document.createElement("input");
      input.placeholder = "confirm";
      const confirmBtn = document.createElement("button");
      confirmBtn.className = "btn danger";
      confirmBtn.textContent = "Confirm";
      confirmBtn.disabled = true;
      input.addEventListener("input", () => {
        confirmBtn.disabled = input.value.trim().toLowerCase() !== "confirm";
      });
      confirmBtn.addEventListener("click", () => decide(true));
      row.appendChild(input);
      row.appendChild(confirmBtn);
      typed.appendChild(row);
      card.appendChild(typed);
    } else {
      const actions = document.createElement("div");
      actions.className = "card-actions";
      const confirmBtn = document.createElement("button");
      confirmBtn.className = "btn primary";
      confirmBtn.textContent = "Confirm";
      confirmBtn.addEventListener("click", () => decide(true));
      const cancel = document.createElement("button");
      cancel.className = "btn ghost";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", () => decide(false));
      actions.appendChild(confirmBtn);
      actions.appendChild(cancel);
      card.appendChild(actions);
    }

    turn.appendChild(card);
    scrollDown();
  }

  /* ---------------------------------------------------------------- */
  /* progress card (DESIGN.md item 7 — shimmer + elapsed timer)        */
  /* ---------------------------------------------------------------- */

  function renderProgressCard(turn, data) {
    const card = document.createElement("div");
    card.className = "card progress";
    card.dataset.callId = data.call_id || "";

    const head = document.createElement("div");
    head.className = "progress-head";
    head.innerHTML =
      `<span>${VapApi.escapeHtml(data.label || "Working…")}</span>` +
      `<span class="elapsed">0s</span>`;
    card.appendChild(head);

    const bar = document.createElement("div");
    bar.className = "shimmer";
    card.appendChild(bar);

    const started = Date.now();
    const timer = setInterval(() => {
      const elapsed = card.querySelector(".elapsed");
      if (!elapsed || !card.isConnected) {
        clearInterval(timer);
        return;
      }
      const secs = Math.round((Date.now() - started) / 1000);
      elapsed.textContent = secs >= 60
        ? `${Math.floor(secs / 60)}m ${secs % 60}s`
        : `${secs}s`;
    }, 1000);
    card.addEventListener("vap-done", () => clearInterval(timer), { once: true });

    turn.appendChild(card);
    scrollDown();
  }

  /** complete any open progress cards (turn end) */
  function settleProgress(turn) {
    turn.querySelectorAll(".card.progress").forEach((card) => {
      card.dispatchEvent(new CustomEvent("vap-done"));
      const bar = card.querySelector(".shimmer");
      if (bar) {
        bar.style.animation = "none";
        bar.style.background = "var(--ok)";
        bar.style.opacity = "0.6";
      }
    });
  }

  /* ---------------------------------------------------------------- */
  /* result cards (DESIGN.md items 8-11)                               */
  /* ---------------------------------------------------------------- */

  function actionButtons(actions) {
    const wrap = document.createElement("div");
    wrap.className = "card-actions";
    for (const action of actions || []) {
      const button = document.createElement("button");
      const isPrimary = action.action === "auto_fix";
      button.className = "btn " + (isPrimary ? "primary" : action.action === "retry" ? "" : "ghost");
      button.textContent = action.label || action.action || "Run";
      button.addEventListener("click", () => {
        wrap.querySelectorAll("button").forEach((b) => (b.disabled = true));
        if (action.action === "open_url" && action.url) {
          window.open(action.url, "_blank", "noopener");
        } else if (action.action === "open_settings") {
          try { chrome.runtime.openOptionsPage(); } catch (_err) { /* ignore */ }
        } else {
          answerRequest({
            conversation_id: state.conversationId,
            action: action.action || "custom",
            payload: action.payload || {},
          });
        }
      });
      wrap.appendChild(button);
    }
    return wrap;
  }

  function renderResultCard(turn, data) {
    const kind = ["success", "info", "error", "security"].includes(data.kind)
      ? data.kind : "info";
    const card = document.createElement("div");
    card.className = "card " + kind;
    card.dataset.kind = kind;

    if (kind === "success") {
      const check = document.createElement("div");
      check.className = "big-check";
      check.innerHTML = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="2.5" stroke-linecap="round"
        stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>`;
      card.appendChild(check);
    } else if (kind === "security") {
      const shield = document.createElement("div");
      shield.className = "shield";
      shield.innerHTML = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none"
        stroke="currentColor" stroke-width="2" stroke-linecap="round"
        stroke-linejoin="round"><path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z"/></svg>`;
      card.appendChild(shield);
    }

    const title = document.createElement("div");
    title.className = "card-title";
    title.textContent = data.title || (kind === "error" ? "That did not work" : "");
    card.appendChild(title);

    const body = document.createElement("div");
    body.className = "card-body md";
    body.innerHTML = VapApi.renderMarkdown(data.body_md || "");
    promoteMonoValues(body);
    card.appendChild(body);

    if (kind === "security") decorateFindings(body);

    if (Array.isArray(data.actions) && data.actions.length) {
      card.appendChild(actionButtons(data.actions));
    }
    turn.appendChild(card);
    scrollDown();
  }

  /** single-line fenced blocks become mono values with copy buttons
   *  (DESIGN.md: domains, connection strings, passwords render in mono
   *  with copy buttons) */
  function promoteMonoValues(body) {
    body.querySelectorAll(".md-code").forEach((block) => {
      const code = block.querySelector("code");
      if (!code) return;
      const text = code.textContent || "";
      if (text.includes("\n") || text.length > 120) return;
      const row = document.createElement("div");
      row.className = "mono-value";
      const value = document.createElement("code");
      value.textContent = text;
      const copy = document.createElement("button");
      copy.className = "md-copy";
      copy.setAttribute("data-copy", text);
      copy.textContent = "Copy";
      row.appendChild(value);
      row.appendChild(copy);
      block.replaceWith(row);
    });
  }

  /** pull `- **high** — finding` style bullets into severity-pill rows */
  function decorateFindings(body) {
    body.querySelectorAll("li").forEach((li) => {
      const text = li.textContent;
      const match = text.match(/^\s*(high|medium|low)\s*[—:-]\s*(.*)$/i);
      if (!match) return;
      const severity = match[1].toLowerCase();
      li.innerHTML = `<span class="pill ${severity === "high" ? "high" : severity === "medium" ? "warn" : "muted"}">${severity}</span>
        <span>${VapApi.escapeHtml(match[2])}</span>`;
    });
  }

  /* ---------------------------------------------------------------- */
  /* error cards (translated — never raw codes)                        */
  /* ---------------------------------------------------------------- */

  function renderErrorCard(turn, code, message) {
    settleProgress(turn);
    const copy = VapApi.errorCopy(code);
    const actions = [];
    if (copy.fix === "open_settings") {
      actions.push({ label: "Open Settings", action: "open_settings" });
    } else if (copy.fix === "retry") {
      actions.push({ label: "Retry", action: "retry" });
    }
    renderResultCard(turn, {
      kind: "error",
      title: copy.title,
      body_md: `${copy.body}${message ? `\n\n> ${message}` : ""}`,
      actions,
    });
  }

  /* ---------------------------------------------------------------- */
  /* /api/answer + /api/audit                                          */
  /* ---------------------------------------------------------------- */

  function answerRequest(payload) {
    const port = chrome.runtime.connect({ name: "fetch-proxy" });
    const requestId = "answer-" + Date.now();
    port.onMessage.addListener((message) => {
      if (message && message.id === requestId &&
          (message.type === "end" || message.type === "error")) {
        try { port.disconnect(); } catch (_err) { /* ignore */ }
      }
    });
    port.postMessage({
      id: requestId,
      url: state.agentUrl + "/api/answer",
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  }

  async function showAudit() {
    hideEmpty();
    const turn = beginTurn();
    const card = document.createElement("div");
    card.className = "card audit";
    card.innerHTML = `<div class="card-title">What I changed</div>
      <div class="card-body"><div class="typing"><span></span><span></span><span></span></div></div>`;
    turn.appendChild(card);
    scrollDown();

    const port = chrome.runtime.connect({ name: "fetch-proxy" });
    const requestId = "audit-" + Date.now();
    let text = "";
    port.onMessage.addListener((message) => {
      if (!message || message.id !== requestId) return;
      if (message.type === "chunk") {
        text += message.text;
      } else if (message.type === "end") {
        try { port.disconnect(); } catch (_err) { /* ignore */ }
        let entries = [];
        try {
          entries = JSON.parse(text);
        } catch (_err) {
          entries = [];
        }
        renderAuditBody(card, Array.isArray(entries) ? entries : []);
      } else if (message.type === "error") {
        try { port.disconnect(); } catch (_err) { /* ignore */ }
        card.querySelector(".card-body").innerHTML =
          `<span class="muted">Could not load the audit trail.</span>`;
      }
    });
    port.postMessage({
      id: requestId,
      url: state.agentUrl + "/api/audit?conversation_id=" +
        encodeURIComponent(state.conversationId || ""),
      method: "GET",
    });
  }

  function renderAuditBody(card, entries) {
    const body = card.querySelector(".card-body");
    if (!entries.length) {
      body.innerHTML = `<div class="muted">Nothing changed yet — every mutation will
        show up here with a timestamp and result.</div>`;
      return;
    }
    const timeline = document.createElement("div");
    timeline.className = "timeline";
    for (const entry of entries) {
      const item = document.createElement("div");
      item.className = "timeline-item";
      const when = String(entry.at || "").slice(11, 19) || "—";
      item.innerHTML =
        `<span class="when">${VapApi.escapeHtml(when)}</span>` +
        `<span class="grow"><strong>${VapApi.escapeHtml(entry.tool || "")}</strong> ` +
        `<span class="muted">${VapApi.escapeHtml(entry.target || "")}</span><br>` +
        `${VapApi.escapeHtml(entry.summary || "")}</span>` +
        `<span class="pill ${entry.ok ? "ok" : "high"}">${entry.ok ? "ok" : "failed"}</span>`;
      timeline.appendChild(item);
    }
    body.innerHTML = "";
    body.appendChild(timeline);
    scrollDown();
  }

  init();
  el.input.focus();
})();
