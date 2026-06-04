"use strict";
/* ============================================================================
   SCM Master — Copilot chat bubble. Loads after the feature files.

   A floating ask-anything assistant wired to POST /agent/ask, which answers
   grounded in a live snapshot of the whole operation (assets, contracts,
   procurement, capacity, inbound, spend, tracking). Self-contained: injects its
   own DOM + styles, reuses app.js helpers (api, esc, icon). On-brand via the
   TrueSpend design tokens.
============================================================================ */
(function () {
  const SUGGESTIONS = [
    "What needs my attention this week?",
    "Which contracts are expiring soon?",
    "Are any locations over capacity?",
    "Where is the biggest spend concentration?",
  ];
  let history = [];   // [{role, content}]
  let busy = false;

  /* ── styles (scoped, token-driven) ───────────────────────────────── */
  const css = `
  .scmchat-fab{position:fixed;right:24px;bottom:24px;z-index:60;width:54px;height:54px;border-radius:999px;
    background:var(--ts-ink-night);color:var(--ts-ink-inverse);border:none;cursor:pointer;display:flex;
    align-items:center;justify-content:center;box-shadow:var(--ts-shadow-lg);transition:transform var(--ts-dur-fast) var(--ts-ease)}
  .scmchat-fab:hover{transform:translateY(-2px)}
  .scmchat-fab__spark{position:absolute;top:10px;right:11px;width:7px;height:7px;border-radius:999px;background:var(--ts-brand-gold)}
  .scmchat-panel{position:fixed;right:24px;bottom:88px;z-index:60;width:392px;max-width:calc(100vw - 32px);
    height:560px;max-height:calc(100vh - 120px);background:var(--ts-surface);border:1px solid var(--ts-line);
    border-radius:var(--ts-radius-lg);box-shadow:var(--ts-shadow-lg);display:flex;flex-direction:column;overflow:hidden;
    opacity:0;transform:translateY(8px) scale(.98);pointer-events:none;transition:opacity var(--ts-dur-med) var(--ts-ease),transform var(--ts-dur-med) var(--ts-ease-emphatic)}
  .scmchat-panel.open{opacity:1;transform:none;pointer-events:auto}
  .scmchat-head{display:flex;align-items:center;gap:10px;padding:14px 16px;border-bottom:1px solid var(--ts-line);background:var(--ts-ink-night);color:var(--ts-ink-inverse)}
  .scmchat-head__t{font-family:var(--ts-font-display);font-size:18px;line-height:1}
  .scmchat-head__s{font-size:11px;letter-spacing:.12em;text-transform:uppercase;color:var(--ts-brand-gold);margin-top:3px}
  .scmchat-x{margin-left:auto;background:none;border:none;color:var(--ts-ink-inverse);opacity:.7;cursor:pointer;font-size:18px;line-height:1}
  .scmchat-x:hover{opacity:1}
  .scmchat-log{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:12px}
  .scmchat-msg{max-width:86%;padding:10px 13px;border-radius:var(--ts-radius-md);font-size:14px;line-height:1.5;white-space:pre-wrap;word-wrap:break-word}
  .scmchat-msg--user{align-self:flex-end;background:var(--ts-brand-gold-wash);color:var(--ts-ink);border:1px solid var(--ts-brand-gold-soft)}
  .scmchat-msg--bot{align-self:flex-start;background:var(--ts-paper-deep);color:var(--ts-ink-soft)}
  .scmchat-msg--err{align-self:flex-start;background:var(--ts-negative-wash);color:var(--ts-negative)}
  .scmchat-empty{color:var(--ts-ink-mute);font-size:13px;line-height:1.6}
  .scmchat-sugg{display:flex;flex-direction:column;gap:7px;margin-top:12px}
  .scmchat-sugg button{text-align:left;background:var(--ts-surface);border:1px solid var(--ts-line);border-radius:var(--ts-radius-sm);
    padding:8px 11px;font-size:13px;color:var(--ts-ink-soft);cursor:pointer}
  .scmchat-sugg button:hover{border-color:var(--ts-brand-gold);color:var(--ts-ink)}
  .scmchat-foot{display:flex;gap:8px;padding:12px;border-top:1px solid var(--ts-line);background:var(--ts-paper)}
  .scmchat-foot input{flex:1;background:var(--ts-surface);border:1px solid var(--ts-line);border-radius:var(--ts-radius-sm);padding:9px 11px;font:inherit;font-size:14px}
  .scmchat-foot button{background:var(--ts-ink-night);color:var(--ts-ink-inverse);border:none;border-radius:var(--ts-radius-sm);padding:0 14px;cursor:pointer}
  .scmchat-foot button:disabled{opacity:.5;cursor:default}
  .scmchat-dots span{display:inline-block;width:5px;height:5px;margin:0 1px;border-radius:999px;background:var(--ts-ink-faint);animation:scmchatb 1s infinite}
  .scmchat-dots span:nth-child(2){animation-delay:.15s}.scmchat-dots span:nth-child(3){animation-delay:.3s}
  @keyframes scmchatb{0%,60%,100%{opacity:.3}30%{opacity:1}}
  @media (prefers-reduced-motion: reduce){.scmchat-panel,.scmchat-fab{transition:none}.scmchat-dots span{animation:none}}`;
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);

  /* ── DOM ──────────────────────────────────────────────────────────── */
  const fab = document.createElement("button");
  fab.className = "scmchat-fab";
  fab.title = "Ask the copilot";
  fab.innerHTML = `<span class="scmchat-fab__spark"></span>
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.5 8.5 0 0 1-12.4 7.6L3 21l1.9-5.6A8.5 8.5 0 1 1 21 11.5z"/></svg>`;

  const panel = document.createElement("div");
  panel.className = "scmchat-panel";
  panel.innerHTML = `
    <div class="scmchat-head">
      <div>
        <div class="scmchat-head__t">Copilot</div>
        <div class="scmchat-head__s">Ask about the operation</div>
      </div>
      <button class="scmchat-x" title="Close">&times;</button>
    </div>
    <div class="scmchat-log" id="scmchat-log"></div>
    <form class="scmchat-foot" id="scmchat-form">
      <input id="scmchat-input" placeholder="Ask anything — assets, contracts, spend…" autocomplete="off" />
      <button type="submit" title="Send">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13M22 2l-7 20-4-9-9-4z"/></svg>
      </button>
    </form>`;

  document.body.appendChild(fab);
  document.body.appendChild(panel);

  const logEl = panel.querySelector("#scmchat-log");
  const inputEl = panel.querySelector("#scmchat-input");

  function renderEmpty() {
    logEl.innerHTML = `<div class="scmchat-empty">
      I'm wired into the live operation — assets, sourcing contracts, purchase orders,
      capacity, inbound deliveries, spend and shipment tracking. Ask me anything.
      <div class="scmchat-sugg">${SUGGESTIONS.map((s) => `<button>${esc(s)}</button>`).join("")}</div>
    </div>`;
    logEl.querySelectorAll(".scmchat-sugg button").forEach((b) =>
      b.addEventListener("click", () => send(b.textContent)));
  }

  function addMsg(text, kind) {
    const d = document.createElement("div");
    d.className = "scmchat-msg scmchat-msg--" + kind;
    d.textContent = text;
    logEl.appendChild(d);
    logEl.scrollTop = logEl.scrollHeight;
    return d;
  }

  async function send(q) {
    q = (q || "").trim();
    if (!q || busy) return;
    if (logEl.querySelector(".scmchat-empty")) logEl.innerHTML = "";
    addMsg(q, "user");
    inputEl.value = "";
    busy = true;
    const thinking = addMsg("", "bot");
    thinking.innerHTML = `<span class="scmchat-dots"><span></span><span></span><span></span></span>`;
    try {
      const res = await api("/agent/ask", { method: "POST", body: { question: q, history } });
      thinking.textContent = res.answer;
      history.push({ role: "user", content: q });
      history.push({ role: "assistant", content: res.answer });
      if (history.length > 12) history = history.slice(-12);
    } catch (e) {
      thinking.className = "scmchat-msg scmchat-msg--err";
      thinking.textContent = (e && e.message) || "The copilot is unavailable right now.";
    } finally {
      busy = false;
      logEl.scrollTop = logEl.scrollHeight;
    }
  }

  function toggle(open) {
    const show = open === undefined ? !panel.classList.contains("open") : open;
    panel.classList.toggle("open", show);
    if (show) {
      if (!logEl.childElementCount) renderEmpty();
      setTimeout(() => inputEl.focus(), 60);
    }
  }

  fab.addEventListener("click", () => toggle());
  panel.querySelector(".scmchat-x").addEventListener("click", () => toggle(false));
  panel.querySelector("#scmchat-form").addEventListener("submit", (e) => { e.preventDefault(); send(inputEl.value); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && panel.classList.contains("open")) toggle(false); });

  // Hide the bubble on the login screen; show once the app shell is visible.
  function syncVisibility() {
    const appVisible = !document.getElementById("app-view")?.classList.contains("hidden");
    fab.style.display = appVisible ? "flex" : "none";
    if (!appVisible) toggle(false);
  }
  syncVisibility();
  new MutationObserver(syncVisibility).observe(
    document.getElementById("app-view") || document.body,
    { attributes: true, attributeFilter: ["class"] });
})();
