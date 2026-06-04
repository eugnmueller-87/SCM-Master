"use strict";
/* ============================================================================
   SCM Master — feature module (loads AFTER app.js; shares its global scope).
   Adds: the agent drawer (/agent/*), the Contracts lifecycle screen
   (/product-suppliers), and a Tweaks control panel (host edit-mode protocol).
   Kept separate so app.js stays the core operations surface.
============================================================================ */

/* ── extra icons (ICONS object lives in app.js) ────────────────────── */
ICONS.contract = '<path d="M6 2h8l4 4v16H6z"/><path d="M14 2v4h4"/><path d="M9 13h6M9 17h6M9 9h2"/>';
ICONS.agent    = '<path d="M12 3l1.6 4.4L18 9l-4.4 1.6L12 15l-1.6-4.4L6 9l4.4-1.6z"/><path d="M18 14l.8 2.2L21 17l-2.2.8L18 20l-.8-2.2L15 17l2.2-.8z"/>';
ICONS.x        = '<path d="M18 6L6 18M6 6l12 12"/>';
ICONS.refresh  = '<path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 4v5h-5"/>';
ICONS.renew    = '<path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/><path d="M3 21v-5h5"/>';

/* ── Contract lifecycle ────────────────────────────────────────────── */
const CONTRACT_LIFECYCLE = ["DRAFT", "ACTIVE", "RENEWAL_DUE", "EXPIRING", "EXPIRED"];
const CONTRACT_SHORT = ["Draft", "Active", "Renewal", "Expiring", "Expired"];
const CONTRACT_STATUS = {
  DRAFT:       { label: "Draft",       tone: "neutral" },
  ACTIVE:      { label: "Active",      tone: "positive" },
  RENEWAL_DUE: { label: "Renewal due", tone: "warning" },
  EXPIRING:    { label: "Expiring",    tone: "warning" },
  EXPIRED:     { label: "Expired",     tone: "mute" },
  SUPERSEDED:  { label: "Superseded",  tone: "mute" },
};
const contractStatusOf = (r) => r.contract_status || (r.active ? "ACTIVE" : "EXPIRED");

// Days until renewal (from term_end). Negative = lapsed.
const renewalDays = (r) => r.term_end ? Math.ceil((new Date(r.term_end) - new Date("2026-05-28T00:00:00Z")) / 86400000) : null;
const renewChip = (r) => {
  const d = renewalDays(r);
  if (d == null) return `<span class="renew-chip"><small>not set</small></span>`;
  if (d < 0) return `<span class="renew-chip renew-chip--lapsed">${Math.abs(d)}<small>days lapsed</small></span>`;
  const soon = d <= 60;
  return `<span class="renew-chip${soon ? " renew-chip--soon" : ""}">${d}<small>days left</small></span>`;
};

/* Annual budget vs YTD spend per contract. Reads real fields when present
   (annual_budget, ytd_spend); else synthesises a plausible plan so the burn
   renders. Budget burns DOWN as spend goes UP. */
function budgetOf(r) {
  const budget = r.annual_budget != null ? Number(r.annual_budget) : Math.round((r.contract_price || 1000) * ({ p1: 30, p2: 22, p3: 220, p4: 10 }[r.product_id] || 24));
  const spent = r.ytd_spend != null ? Number(r.ytd_spend)
    : (contractStatusOf(r) === "DRAFT" ? 0
      : Math.round(budget * ({ ps1: 0.71, ps2: 0.34, ps3: 0.58, ps4: 0.27, ps5: 0.83, ps6: 1.04, ps7: 0 }[r.id] ?? 0.5)));
  const remaining = budget - spent;
  const pct = budget ? Math.min(spent / budget, 1.2) : 0;
  return { budget, spent, remaining, pct, over: spent > budget };
}

/* register the nav item (before init runs in the host page) */
(function () {
  const i = NAV.findIndex((n) => n.id === "capacity");
  NAV.splice(i + 1, 0, { id: "contracts", label: "Contracts", icon: "contract" });
})();
CRUMBS.contracts = "Contracts";

let contractCache = [];
let openContractId = null;

RENDER.contracts = async function () {
  $("#screen").innerHTML = `
    ${pageHead("Sourcing", "Contracts", "Every supplier source is a sourcing contract — price, lead time and minimum order, tracked through its life from draft to expiry.", "")}
    <div class="toolbar"><div class="toolbar__spacer"></div><span class="toolbar__count" id="contract-count"></span></div>
    <div class="panel"><table class="tbl">
      <thead><tr><th>Supplier</th><th>Scope</th><th class="num">Contract price</th><th style="width:210px">YTD spend vs budget</th><th class="num">Renewal</th><th>Status</th><th style="width:32px"></th></tr></thead>
      <tbody id="contract-rows"><tr><td colspan="7"><div class="state"><div class="state__sub">Loading…</div></div></td></tr></tbody>
    </table></div>`;
  try {
    const rows = await api("/product-suppliers?limit=1000");
    contractCache = rows;
    $("#contract-count").textContent = `${rows.length} contract${rows.length === 1 ? "" : "s"} across ${new Set(rows.map((r) => r.supplier_id)).size} suppliers`;
    const tb = $("#contract-rows");
    if (!rows.length) { tb.innerHTML = `<tr><td colspan="7"><div class="state"><div class="state__icon">${icon("contract", 22)}</div><div class="state__sub">No sourcing contracts on file.</div></div></td></tr>`; return; }
    tb.innerHTML = rows.map((r) => {
      const sup = (ORGS[r.supplier_id] || {}).name || "—";
      const prod = PRODUCTS[r.product_id] || {};
      const st = contractStatusOf(r);
      const m = CONTRACT_STATUS[st] || CONTRACT_STATUS.DRAFT;
      const pref = r.preference_rank != null && r.preference_rank <= 1;
      const b = budgetOf(r);
      const fill = b.over ? "var(--ts-negative)" : b.pct > 0.85 ? "var(--ts-warning)" : "var(--ts-brand-gold)";
      const budgetCell = `<div class="budget-wrap">
        <div class="budget-bar"><div class="budget-bar__spent" style="width:${Math.min(b.pct, 1) * 100}%;background:${fill}"></div></div>
        <div class="budget-nums"><span class="spent">${euro(b.spent)} spent</span><span class="left">${b.over ? euro(-b.remaining) + " over" : euro(b.remaining) + " left"}</span></div>
      </div>`;
      return `<tr class="clickable" data-cid="${r.id}">
        <td><div class="cell-prod"><span class="cell-prod__icon">${icon("contract", 16)}</span>
          <div><div class="cell-prod__name">${esc(sup)}</div>${pref ? `<div class="cell-prod__cat">Preferred source</div>` : `<div class="cell-prod__cat">Rank ${r.preference_rank ?? "—"}</div>`}</div></div></td>
        <td>${productCell(r.product_id, false)}</td>
        <td class="num"><span class="prov__v money">${euro(r.contract_price)}</span></td>
        <td>${budgetCell}</td>
        <td class="num">${renewChip(r)}</td>
        <td>${plainPill(m.label, m.tone)}</td>
        <td style="color:var(--ts-ink-faint)"><span class="chev" style="display:inline-flex;transition:transform 160ms">${icon("chev", 15)}</span></td>
      </tr>
      <tr class="brief-host" data-chost="${r.id}" hidden><td colspan="7"></td></tr>`;
    }).join("");
    $$("#contract-rows tr.clickable").forEach((tr) => tr.addEventListener("click", () => toggleContract(tr.dataset.cid)));
  } catch (e) {
    $("#contract-rows").innerHTML = `<tr><td colspan="7">${errState(e.message)}</td></tr>`;
  }
};

function toggleContract(id) {
  const host = $(`#contract-rows tr[data-chost="${id}"]`);
  const row = $(`#contract-rows tr.clickable[data-cid="${id}"]`);
  const was = openContractId === id;
  $$('#contract-rows tr.brief-host').forEach((h) => { h.hidden = true; h.firstElementChild.innerHTML = ""; h.classList.remove("brief"); });
  $$('#contract-rows tr.clickable').forEach((r) => { r.classList.remove("is-open"); const c = r.querySelector(".chev"); if (c) c.style.transform = "none"; });
  if (was) { openContractId = null; return; }
  openContractId = id;
  row.classList.add("is-open");
  const chev = row.querySelector(".chev"); if (chev) chev.style.transform = "rotate(90deg)";
  host.hidden = false; host.classList.add("brief");
  host.firstElementChild.innerHTML = renderContractBrief(contractCache.find((c) => c.id === id));
  $$(`#contract-rows [data-caction]`).forEach((b) => b.addEventListener("click", (ev) => { ev.stopPropagation(); toast(b.dataset.caction === "renew" ? "Renewal drafted — pending procurement sign-off" : "Re-source flow opens the ranked alternatives", "ok"); }));
}

function renderContractBrief(r) {
  const st = contractStatusOf(r);
  const idx = Math.max(0, CONTRACT_LIFECYCLE.indexOf(st === "SUPERSEDED" ? "EXPIRED" : st));
  const stepper = `<div class="stepper">${CONTRACT_LIFECYCLE.map((s, i) => {
    const cls = i < idx ? "step--done" : i === idx ? "step--current" : "";
    return `<div class="step ${cls}"><div class="step__node"><div class="step__dot"></div></div>${i < CONTRACT_LIFECYCLE.length - 1 ? '<div class="step__bar"></div>' : ""}</div>`;
  }).join("")}</div>
  <div class="steplabels">${CONTRACT_SHORT.map((s, i) => `<div class="steplabel${i === idx ? " steplabel--current" : ""}">${s}</div>`).join("")}</div>`;

  const sup = (ORGS[r.supplier_id] || {}).name || "—";
  const prod = PRODUCTS[r.product_id] || {};
  const rows = [
    ["Supplier", esc(sup)],
    ["Product scope", esc(prod.name || r.product_id)],
    ["Contract price", `<span class="prov__v money">${euro(r.contract_price)} ${esc(r.currency_code || "")}</span>`],
    ["Standard lead time", r.standard_lead_time_days != null ? r.standard_lead_time_days + " days" : "—"],
    ["Minimum order qty", r.min_order_quantity ?? "—"],
    ["Preference rank", r.preference_rank ?? "—"],
    ["Supplier SKU", `<span class="ref">${esc(r.supplier_product_code || "—")}</span>`],
    ["Term", r.term_start || r.term_end ? `${fmtDate(r.term_start)} → ${fmtDate(r.term_end)}` : "Not yet modelled"],
  ];
  const renewal = r.term_end ? (() => {
    const d = renewalDays(r);
    return d < 0 ? `Lapsed ${Math.abs(d)} days ago` : `Renews in ${d} days`;
  })() : null;

  // budget burn — cumulative monthly spend climbing toward the annual budget line
  const b = budgetOf(r);
  const months = ["Jan", "Feb", "Mar", "Apr", "May"];
  const curve = [0.16, 0.34, 0.55, 0.78, 1.0];           // share of YTD spend by month
  const burnFill = b.over ? "var(--ts-negative)" : "var(--ts-brand-gold)";
  const burn = `<div class="burn">${months.map((m, i) => {
    const cum = b.spent * curve[i];
    const h = b.budget ? Math.min(cum / b.budget, 1) * 100 : 0;
    return `<div class="burn__col" title="${m}: ${euro(cum)} cumulative"><div class="burn__spend" style="height:${h}%;background:${burnFill}"></div></div>`;
  }).join("")}</div>
  <div class="burn__labels">${months.map((m) => `<span>${m}</span>`).join("")}</div>`;

  const budgetPanel = `
    <div class="brief__h" style="margin-top:24px">Budget · ${new Date().getFullYear()}</div>
    <div class="budget-bar" style="height:14px"><div class="budget-bar__spent" style="width:${Math.min(b.pct, 1) * 100}%;background:${burnFill}"></div></div>
    <div class="budget-nums" style="font-size:12px;margin-top:7px">
      <span class="spent">${euro(b.spent)} spent · ${Math.round(b.pct * 100)}%</span>
      <span class="left">${b.over ? euro(-b.remaining) + " over budget" : euro(b.remaining) + " remaining"} of ${euro(b.budget)}</span>
    </div>
    ${burn}`;

  return `<div class="brief__inner fade-in">
    <div>
      <div class="brief__h">Contract lifecycle</div>${stepper}
      ${renewal ? `<div class="insight__meta" style="margin-top:16px">${esc(renewal)} · ${esc(CONTRACT_STATUS[st].label)}</div>` : ""}
      ${budgetPanel}
      <div class="brief__actions" style="margin-top:18px">
        <span class="brief__actions-label">Actions</span>
        <button class="btn btn--secondary btn--sm" data-caction="renew">${icon("renew", 13)} Draft renewal</button>
        <button class="btn btn--ghost btn--sm" data-caction="resource">Re-source</button>
      </div>
    </div>
    <div>
      <div class="brief__h">Terms</div>
      <div class="prov">${rows.map(([k, v], i) => `<div class="prov__row"${i === rows.length - 1 ? ' style="border-bottom:none"' : ""}><span class="prov__k">${k}</span><span class="prov__v">${v}</span></div>`).join("")}</div>
    </div>
  </div>`;
}

/* ── Agent drawer ──────────────────────────────────────────────────── */
const SEVERITY = {
  info:   { label: "Info",   tone: "info" },
  watch:  { label: "Watch",  tone: "warning" },
  action: { label: "Action", tone: "negative" },
};
const TIER = {
  act:      { label: "Auto-execute", tone: "positive" },
  propose:  { label: "Proposed",     tone: "warning" },
  escalate: { label: "Escalate",     tone: "negative" },
};
const AUTONOMY = { suggest: "Suggest only", low: "Auto-close low-risk", full: "Full autonomy" };

let drawerEl, scrimEl, lastRun = null;

function buildDrawer() {
  scrimEl = document.createElement("div");
  scrimEl.className = "scrim";
  scrimEl.addEventListener("click", closeAgent);
  drawerEl = document.createElement("div");
  drawerEl.className = "drawer";
  document.body.appendChild(scrimEl);
  document.body.appendChild(drawerEl);
}

function injectAgentButton() {
  const right = $(".topbar__right");
  if (!right) return;
  const btn = document.createElement("button");
  btn.className = "agent-btn";
  btn.innerHTML = `${icon("agent", 15)} Agent`;
  btn.addEventListener("click", openAgent);
  right.insertBefore(btn, right.firstChild);
}

function autonomyLabel() { return AUTONOMY[(readTweaks().autonomy)] || AUTONOMY.low; }

function openAgent() {
  if (!drawerEl) buildDrawer();
  drawerEl.innerHTML = `
    <div class="drawer__head">
      <div class="drawer__mark">${icon("agent", 16)}</div>
      <div><div class="drawer__title">Agent</div><div class="drawer__sub">Surfaces what needs you; closes the rest.</div></div>
      <span class="drawer__autonomy" id="agent-autonomy">${esc(autonomyLabel())}</span>
      <button class="iconbtn" id="agent-close" style="margin-left:6px">${icon("x", 16)}</button>
    </div>
    <div class="drawer__body" id="agent-body">
      <div class="drawer__section">
        <div class="drawer__sectionhead">This week · insights</div>
        <div id="agent-insights"><div class="state"><div class="state__sub">Reading the portfolio…</div></div></div>
      </div>
      <div class="drawer__section">
        <div class="drawer__sectionhead">Weekly purchasing run</div>
        <div id="agent-run">
          <p class="insight__finding" style="margin-bottom:12px">Preview the buys the agent would make this period. Nothing is placed until you approve it.</p>
          <button class="btn btn--ink btn--sm" id="agent-run-btn">${icon("refresh", 13)} Run preview</button>
        </div>
      </div>
    </div>
    <div class="drawer__foot" id="agent-foot" hidden></div>`;
  requestAnimationFrame(() => { scrimEl.classList.add("open"); drawerEl.classList.add("open"); });
  // also reveal synchronously (rAF can be throttled in backgrounded frames)
  void drawerEl.offsetWidth;
  scrimEl.classList.add("open"); drawerEl.classList.add("open");
  $("#agent-close").addEventListener("click", closeAgent);
  $("#agent-run-btn").addEventListener("click", runPurchasing);
  loadInsights();
}
function closeAgent() { if (drawerEl) { drawerEl.classList.remove("open"); scrimEl.classList.remove("open"); } }

async function loadInsights() {
  try {
    const list = await api("/agent/insights");
    $("#agent-insights").innerHTML = list.map((it) => {
      const sev = SEVERITY[it.severity] || SEVERITY.info;
      const t = TONE[sev.tone];
      return `<div class="insight">
        <div class="insight__top">${plainPill(sev.label, sev.tone)}<span class="insight__title">${esc(it.title)}</span></div>
        <div class="insight__finding">${esc(it.finding)}</div>
        ${(it.evidence || []).length ? `<div class="insight__ev">${it.evidence.map((e) => `<div class="insight__ev-item">${esc(e)}</div>`).join("")}</div>` : ""}
        <div class="insight__meta">Assumption — ${esc(it.assumption || "—")} · Limit — ${esc(it.limitation || "—")}</div>
        <div class="conf" style="margin-top:8px"><span class="conf__track"><span class="conf__fill" style="width:${Math.round((it.confidence || 0) * 100)}%"></span></span><span class="conf__n">${Math.round((it.confidence || 0) * 100)}% confidence</span></div>
      </div>`;
    }).join("") || `<div class="insight__finding" style="color:var(--ts-ink-faint)">Nothing needs surfacing right now.</div>`;
  } catch (e) {
    $("#agent-insights").innerHTML = `<div class="insight__finding" style="color:var(--ts-negative)">${esc(e.message)}</div>`;
  }
}

async function runPurchasing() {
  const host = $("#agent-run");
  host.innerHTML = `<div class="state"><div class="state__sub">Computing the run…</div></div>`;
  try {
    const res = await api("/agent/purchasing-run", { method: "POST", body: { dry_run: true, period_days: 7 } });
    lastRun = res;
    renderRun(res);
  } catch (e) {
    host.innerHTML = `<div class="insight__finding" style="color:var(--ts-negative)">${esc(e.message)}</div>
      <button class="btn btn--ink btn--sm" id="agent-run-btn" style="margin-top:10px">${icon("refresh", 13)} Retry</button>`;
    $("#agent-run-btn").addEventListener("click", runPurchasing);
  }
}

function renderRun(res) {
  const decisions = res.decisions || [];
  $("#agent-run").innerHTML = decisions.map((d) => {
    const tier = TIER[d.tier] || TIER.propose;
    const sup = (ORGS[d.supplier_id] || {}).name || d.supplier_id || "—";
    const prod = (PRODUCTS[d.product_id] || {}).name || d.product_id;
    const approvable = d.tier !== "escalate";
    return `<div class="decision">
      <div class="decision__top">${plainPill(tier.label, tier.tone)}<span class="decision__name">${esc(prod)}</span><span class="decision__total money">${euro(d.total)}</span></div>
      <div class="decision__sup" style="margin-bottom:6px">${esc(sup)} · ${d.qty} × ${euro(d.unit_price)}</div>
      <div class="decision__rat">${esc(d.rationale || "")}</div>
      <div class="decision__foot">
        <span class="decision__trigger">${esc((d.trigger || {}).type || "").replace(/_/g, " ")} · ${Math.round((d.confidence || 0) * 100)}%</span>
        <label class="decision__check">${approvable ? `<input type="checkbox" data-sup="${esc(d.supplier_id)}" ${d.tier === "act" ? "checked" : ""}/> approve` : `<span style="color:var(--ts-ink-faint)">needs sign-off</span>`}</label>
      </div>
    </div>`;
  }).join("") || `<div class="insight__finding" style="color:var(--ts-ink-faint)">No buys justified this period.</div>`;
  const foot = $("#agent-foot");
  foot.hidden = false;
  const s = res.summary || {};
  foot.innerHTML = `<span class="muted">${s.acted || 0} auto · ${s.proposed || 0} proposed · ${s.escalated || 0} escalated</span>
    <button class="btn btn--primary btn--sm" id="agent-place" style="margin-left:auto">Place approved</button>`;
  $("#agent-place").addEventListener("click", placeApproved);
}

async function placeApproved() {
  const sups = $$("#agent-run input[type=checkbox]:checked").map((c) => c.dataset.sup);
  if (!sups.length) { toast("Select at least one supplier to place", "err"); return; }
  try {
    const res = await api("/agent/purchasing-run/confirm", { method: "POST", body: { approve_suppliers: sups, period_days: 7 } });
    const placed = (res.summary || {}).placed ?? sups.length;
    toast(`Placed ${placed} purchase order${placed === 1 ? "" : "s"}`, "ok");
    closeAgent();
    if (typeof primeCounts === "function") primeCounts();
    if (currentTab === "inbound" || currentTab === "overview") showTab(currentTab);
  } catch (e) { toast(e.message, "err"); }
}

/* ── Tweaks panel (host edit-mode protocol) ────────────────────────── */
const ACCENTS = {
  "#B07219": { gold: "#B07219", deep: "#8F5C12", soft: "#E9DAB5", wash: "#F7EFDE" },
  "#2B5F7A": { gold: "#2B5F7A", deep: "#214B61", soft: "#D2DFE6", wash: "#E6EEF2" },
  "#3D7A5A": { gold: "#3D7A5A", deep: "#2F6147", soft: "#DCE8DF", wash: "#EEF3EE" },
  "#7A4E2B": { gold: "#7A4E2B", deep: "#5F3C20", soft: "#E4D3C2", wash: "#F3EADF" },
};
const TW_DEFAULTS = { accent: "#B07219", density: "comfortable", autonomy: "low" };
const readTweaks = () => { try { return Object.assign({}, TW_DEFAULTS, JSON.parse(localStorage.getItem("scm_tweaks") || "{}")); } catch (e) { return Object.assign({}, TW_DEFAULTS); } };
const writeTweaks = (t) => localStorage.setItem("scm_tweaks", JSON.stringify(t));

function applyTweaks(t) {
  const a = ACCENTS[t.accent] || ACCENTS["#B07219"];
  const r = document.documentElement.style;
  r.setProperty("--ts-brand-gold", a.gold);
  r.setProperty("--ts-brand-gold-deep", a.deep);
  r.setProperty("--ts-brand-gold-soft", a.soft);
  r.setProperty("--ts-brand-gold-wash", a.wash);
  const app = $("#app-view"); if (app) app.setAttribute("data-density", t.density);
  const badge = $("#agent-autonomy"); if (badge) badge.textContent = autonomyLabel();
}

let twEl;
function buildTweaks() {
  const t = readTweaks();
  twEl = document.createElement("div");
  twEl.className = "tw";
  twEl.innerHTML = `
    <div class="tw__head"><span class="tw__title">Tweaks</span><button class="iconbtn" id="tw-close" style="width:26px;height:26px">${icon("x", 15)}</button></div>
    <div class="tw__row">
      <div class="tw__label">Signal accent</div>
      <div class="tw__swatches">${Object.keys(ACCENTS).map((hex) => `<div class="tw__swatch${t.accent === hex ? " sel" : ""}" data-accent="${hex}" style="background:${ACCENTS[hex].gold}"></div>`).join("")}</div>
    </div>
    <div class="tw__row">
      <div class="tw__label">Table density</div>
      <div class="tw__seg" data-group="density">${["comfortable", "compact"].map((d) => `<button data-density="${d}" class="${t.density === d ? "sel" : ""}">${d[0].toUpperCase() + d.slice(1)}</button>`).join("")}</div>
    </div>
    <div class="tw__row">
      <div class="tw__label">Agent autonomy</div>
      <div class="tw__seg" data-group="autonomy">${[["suggest", "Suggest"], ["low", "Low-risk"], ["full", "Full"]].map(([v, l]) => `<button data-autonomy="${v}" class="${t.autonomy === v ? "sel" : ""}">${l}</button>`).join("")}</div>
    </div>`;
  document.body.appendChild(twEl);

  twEl.querySelectorAll(".tw__swatch").forEach((s) => s.addEventListener("click", () => {
    const t2 = readTweaks(); t2.accent = s.dataset.accent; writeTweaks(t2); applyTweaks(t2);
    twEl.querySelectorAll(".tw__swatch").forEach((x) => x.classList.toggle("sel", x === s));
  }));
  twEl.querySelectorAll(".tw__seg").forEach((seg) => seg.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    const key = seg.dataset.group; const t2 = readTweaks(); t2[key] = b.dataset[key]; writeTweaks(t2); applyTweaks(t2);
    seg.querySelectorAll("button").forEach((x) => x.classList.toggle("sel", x === b));
  })));
  $("#tw-close").addEventListener("click", () => { twEl.classList.remove("open"); try { parent.postMessage({ type: "__edit_mode_dismissed" }, "*"); } catch (e) {} });
}
function openTweaks() { if (!twEl) buildTweaks(); void twEl.offsetWidth; twEl.classList.add("open"); }
function closeTweaks() { if (twEl) twEl.classList.remove("open"); }

/* ── wire up at load (before host calls __scmInit) ─────────────────── */
applyTweaks(readTweaks());
injectAgentButton();
window.addEventListener("message", (e) => {
  const ty = e.data && e.data.type;
  if (ty === "__activate_edit_mode") openTweaks();
  else if (ty === "__deactivate_edit_mode") closeTweaks();
});
try { parent.postMessage({ type: "__edit_mode_available" }, "*"); } catch (e) {}
