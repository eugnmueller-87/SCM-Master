"use strict";
/* ============================================================================
   SCM Master — feature module (loads AFTER app.js; shares its global scope).
   Adds: the Contracts lifecycle screen
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

CRUMBS.contracts = "Contracts";

let contractCache = [];
let openContractId = null;

RENDER.contracts = async function () {
  $("#screen").innerHTML = `
    ${pageHead("Sourcing", "Contracts", "Every supplier source is a sourcing contract — price, lead time and minimum order, tracked through its life from draft to expiry.", "")}
    <div class="toolbar"><button class="btn btn--ink" id="onboard-supplier-btn">${icon("plus", 14)} Onboard supplier</button><div class="toolbar__spacer"></div><span class="toolbar__count" id="contract-count"></span></div>
    <div id="onboard-modal-host"></div>
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
  const ob = $("#onboard-supplier-btn");
  if (ob) ob.addEventListener("click", openOnboardWizard);
};

/* ── Supplier onboarding wizard — risk + DPA/NDA gate before a supplier can be
   ordered from. Reuses the New-Order modal chrome (.ordm). Four steps:
   1 details → 2 risk → 3 agreements → 4 review + approve. ─────────────────── */
let _onboard = null;   // {id, step, name, risk_level, dpa, nda}

async function openOnboardWizard() {
  _onboard = { id: null, step: 1, name: "", code: "", risk_level: "", risk_notes: "", dpa: false, nda: false, dpaRef: "", ndaRef: "" };
  renderOnboard();
}

function closeOnboard() { const h = $("#onboard-modal-host"); if (h) h.innerHTML = ""; _onboard = null; }

const ONBOARD_STEPS = ["Supplier", "Risk", "Agreements", "Review"];

function renderOnboard() {
  const host = $("#onboard-modal-host"); if (!host || !_onboard) return;
  const s = _onboard;
  const stepper = ONBOARD_STEPS.map((label, i) => {
    const n = i + 1;
    const cls = n < s.step ? "step--done" : n === s.step ? "step--current" : "";
    return `<div class="ob-step ${cls}"><span class="ob-step__n">${n < s.step ? "✓" : n}</span>${esc(label)}</div>`;
  }).join('<span class="ob-step__sep"></span>');

  host.innerHTML = `<div class="ordm__veil" id="ob-veil"></div>
    <div class="ordm" role="dialog" aria-label="Onboard supplier">
      <div class="ordm__head">
        <div><div class="ordm__eyebrow">Compliance · Sourcing</div><h2 class="ordm__title">Onboard supplier</h2></div>
        <button class="ordm__x" id="ob-x">×</button>
      </div>
      <div class="ob-steps">${stepper}</div>
      <div class="ordm__body" id="ob-body">${onboardStepBody()}</div>
      <div class="ordm__foot">
        <span class="muted" id="ob-hint">${onboardHint()}</span>
        <span style="flex:1"></span>
        ${s.step > 1 ? `<button class="btn btn--ghost" id="ob-back">Back</button>` : `<button class="btn btn--ghost" id="ob-cancel">Cancel</button>`}
        <button class="btn btn--ink" id="ob-next">${s.step < 4 ? "Continue" : `${icon("shield", 14)} Approve &amp; activate`}</button>
      </div>
    </div>`;

  $("#ob-x").onclick = closeOnboard;
  $("#ob-veil").onclick = closeOnboard;
  const cancel = $("#ob-cancel"); if (cancel) cancel.onclick = closeOnboard;
  const back = $("#ob-back"); if (back) back.onclick = () => { _onboard.step--; renderOnboard(); };
  $("#ob-next").onclick = onboardNext;
  wireOnboardInputs();
}

function onboardStepBody() {
  const s = _onboard;
  if (s.step === 1) return `
    <div class="ordm__row"><label>Supplier name</label>
      <input id="ob-name" value="${esc(s.name)}" placeholder="e.g. Acme Components GmbH" autofocus/></div>
    <div class="ordm__row"><label>Code <span class="muted">(optional)</span></label>
      <input id="ob-code" value="${esc(s.code)}" placeholder="e.g. ACME"/></div>
    <p class="muted" style="margin:4px 2px 0">The supplier starts in <b>DRAFT</b> — not orderable until onboarding completes.</p>`;
  if (s.step === 2) return `
    <div class="ordm__row"><label>Risk level</label>
      <div class="ob-risk">${["LOW", "MEDIUM", "HIGH"].map((r) => `
        <button type="button" class="ob-chip ${s.risk_level === r ? "is-on" : ""}" data-risk="${r}">${r[0] + r.slice(1).toLowerCase()}</button>`).join("")}</div></div>
    <div class="ordm__row"><label>Assessment notes <span class="muted">(optional)</span></label>
      <textarea id="ob-notes" rows="3" placeholder="Single-region? PII handling? Financial stability?">${esc(s.risk_notes)}</textarea></div>`;
  if (s.step === 3) return `
    <p class="muted" style="margin:0 2px 10px">Both agreements must be signed. Recorded as documents of record (reference + date) — no files stored.</p>
    <div class="ob-doc ${s.dpa ? "is-on" : ""}" data-doc="dpa">
      <label class="ob-doc__check"><input type="checkbox" id="ob-dpa" ${s.dpa ? "checked" : ""}/> <b>DPA</b> — Data Processing Agreement signed</label>
      <input id="ob-dpa-ref" class="ob-doc__ref" value="${esc(s.dpaRef)}" placeholder="reference / filename / signer" ${s.dpa ? "" : "disabled"}/>
    </div>
    <div class="ob-doc ${s.nda ? "is-on" : ""}" data-doc="nda">
      <label class="ob-doc__check"><input type="checkbox" id="ob-nda" ${s.nda ? "checked" : ""}/> <b>NDA</b> — Non-Disclosure Agreement signed</label>
      <input id="ob-nda-ref" class="ob-doc__ref" value="${esc(s.ndaRef)}" placeholder="reference / filename / signer" ${s.nda ? "" : "disabled"}/>
    </div>`;
  // step 4 — review
  const ok = s.name && s.risk_level && s.dpa && s.nda;
  return `
    <table class="ob-review">
      <tr><td>Supplier</td><td><b>${esc(s.name) || "—"}</b>${s.code ? ` <span class="muted">(${esc(s.code)})</span>` : ""}</td></tr>
      <tr><td>Risk level</td><td>${s.risk_level ? plainPill(s.risk_level, s.risk_level === "HIGH" ? "neg" : s.risk_level === "MEDIUM" ? "warn" : "ok") : '<span class="muted">not assessed</span>'}</td></tr>
      <tr><td>DPA</td><td>${s.dpa ? `✓ signed${s.dpaRef ? ` <span class="muted">· ${esc(s.dpaRef)}</span>` : ""}` : '<span class="muted">— not signed</span>'}</td></tr>
      <tr><td>NDA</td><td>${s.nda ? `✓ signed${s.ndaRef ? ` <span class="muted">· ${esc(s.ndaRef)}</span>` : ""}` : '<span class="muted">— not signed</span>'}</td></tr>
    </table>
    <p class="muted" style="margin:10px 2px 0">${ok ? "All checks satisfied — approving will make this supplier orderable." : "Onboarding incomplete — go back and complete every step before approving."}</p>`;
}

function onboardHint() {
  const s = _onboard;
  return ["Step 1 of 4 · supplier details", "Step 2 of 4 · risk assessment", "Step 3 of 4 · agreements", "Step 4 of 4 · review & approve"][s.step - 1];
}

async function onboardNext() {
  const s = _onboard;
  try {
    if (s.step === 1) {
      s.name = $("#ob-name").value.trim(); s.code = $("#ob-code").value.trim();
      if (!s.name) { toast("Supplier name is required", "err"); return; }
      if (!s.id) {                                   // create on first advance
        const org = await api("/suppliers/onboard", { method: "POST", body: { name: s.name, code: s.code || null } });
        s.id = org.id;
      }
      s.step = 2; renderOnboard(); wireOnboardInputs(); return;
    }
    if (s.step === 2) {
      if (!s.risk_level) { toast("Pick a risk level", "err"); return; }
      s.risk_notes = ($("#ob-notes") || {}).value || "";
      await api(`/suppliers/${s.id}/risk-assessment`, { method: "POST", body: { risk_level: s.risk_level, risk_notes: s.risk_notes || null } });
      s.step = 3; renderOnboard(); wireOnboardInputs(); return;
    }
    if (s.step === 3) {
      s.dpa = $("#ob-dpa").checked; s.nda = $("#ob-nda").checked;
      s.dpaRef = ($("#ob-dpa-ref") || {}).value || ""; s.ndaRef = ($("#ob-nda-ref") || {}).value || "";
      if (!s.dpa || !s.nda) { toast("Both DPA and NDA must be signed", "err"); return; }
      await api(`/suppliers/${s.id}/documents/dpa`, { method: "POST", body: { signed: true, reference: s.dpaRef || null } });
      await api(`/suppliers/${s.id}/documents/nda`, { method: "POST", body: { signed: true, reference: s.ndaRef || null } });
      s.step = 4; renderOnboard(); wireOnboardInputs(); return;
    }
    // step 4 — approve
    await api(`/suppliers/${s.id}/approve`, { method: "POST" });
    toast(`${s.name} onboarded — now orderable`, "ok");
    closeOnboard();
    if (typeof ORGS === "object") ORGS[s.id] = { name: s.name, is_supplier: true };
  } catch (e) {
    toast(e.message || "Onboarding step failed", "err");
  }
}

function wireOnboardInputs() {
  const s = _onboard;
  $$("[data-risk]").forEach((b) => b.onclick = () => { s.risk_level = b.dataset.risk; $$("[data-risk]").forEach((x) => x.classList.toggle("is-on", x === b)); });
  const dpa = $("#ob-dpa"); if (dpa) dpa.onchange = () => { const r = $("#ob-dpa-ref"); if (r) r.disabled = !dpa.checked; $(".ob-doc[data-doc='dpa']").classList.toggle("is-on", dpa.checked); };
  const nda = $("#ob-nda"); if (nda) nda.onchange = () => { const r = $("#ob-nda-ref"); if (r) r.disabled = !nda.checked; $(".ob-doc[data-doc='nda']").classList.toggle("is-on", nda.checked); };
}

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
  const cdata = contractCache.find((c) => c.id === id);
  host.firstElementChild.innerHTML = renderContractBrief(cdata);
  $$(`#contract-rows [data-caction]`).forEach((b) => b.addEventListener("click", (ev) => { ev.stopPropagation(); toast(b.dataset.caction === "renew" ? "Renewal drafted — pending procurement sign-off" : "Re-source flow opens the ranked alternatives", "ok"); }));

  // Contract files (optional per-supplier PDF repository).
  const orgId = cdata && cdata.supplier_id;
  if (orgId) {
    loadContractFiles(orgId);
    const up = $(`[data-cfile-upload="${orgId}"]`);
    if (up) up.addEventListener("click", (ev) => { ev.stopPropagation(); uploadContractFile(orgId); });
    // Download/remove buttons are rendered ASYNC by loadContractFiles, so delegate
    // from the (already-present) files host rather than binding each button.
    const host2 = $(`[data-cfiles="${orgId}"]`);
    if (host2) host2.addEventListener("click", (ev) => {
      const dl = ev.target.closest("[data-cfile-dl]");
      const rm = ev.target.closest("[data-cfile-rm]");
      if (dl) { ev.stopPropagation(); const [o, d, f] = dl.dataset.cfileDl.split("|"); downloadContractFile(o, d, f); }
      else if (rm) { ev.stopPropagation(); const [o, d] = rm.dataset.cfileRm.split("|"); removeContractFile(o, d); }
    });
  }
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
      <div class="brief__h" style="margin-top:24px">Contract files</div>
      <div id="cfiles-${r.supplier_id}" data-cfiles="${r.supplier_id}">
        <div class="state state--sm"><div class="state__sub">Loading…</div></div>
      </div>
      <div class="cfiles-upload" style="margin-top:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <input type="file" accept="application/pdf" data-cfile-input="${r.supplier_id}" />
        <input type="text" class="input input--sm" placeholder="kind (NDA/DPA/POC/MSA)" data-cfile-kind="${r.supplier_id}" style="width:170px" />
        <button class="btn btn--secondary btn--sm" data-cfile-upload="${r.supplier_id}">${icon("plus", 13)} Upload PDF</button>
      </div>
    </div>
  </div>`;
}

/* ── Contract files: optional per-supplier PDF repository ───────────────
   The shared api() helper does JSON only, so upload (multipart) and download
   (blob, with the Bearer header) use raw fetch. List + delete go through api().*/
async function loadContractFiles(orgId) {
  const host = $(`#cfiles-${CSS.escape(orgId)}`) || $(`[data-cfiles="${orgId}"]`);
  if (!host) return;
  let docs = [];
  try { docs = await api(`/suppliers/${orgId}/contracts`); }
  catch (e) { host.innerHTML = `<div class="muted" style="font-size:13px">Couldn't load contracts.</div>`; return; }
  if (!docs.length) {
    host.innerHTML = `<div class="muted" style="font-size:13px">No contract files on record.</div>`;
    return;
  }
  host.innerHTML = docs.map((d) => `
    <div class="cfile-row" style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--ts-line)">
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
        ${esc(d.original_filename)}${d.kind ? ` <span class="ref">${esc(d.kind)}</span>` : ""}
        <span class="muted" style="font-size:11px"> · ${(d.size_bytes / 1024).toFixed(0)} KB · ${fmtDate(d.uploaded_at)}</span>
      </span>
      <button class="btn btn--ghost btn--sm" data-cfile-dl="${orgId}|${d.id}|${esc(d.original_filename)}">Download</button>
      <button class="btn btn--ghost btn--sm" data-cfile-rm="${orgId}|${d.id}">Remove</button>
    </div>`).join("");
}

async function uploadContractFile(orgId) {
  const input = $(`[data-cfile-input="${orgId}"]`);
  const kind = ($(`[data-cfile-kind="${orgId}"]`) || {}).value || "";
  const f = input && input.files && input.files[0];
  if (!f) { toast("Choose a PDF first", "err"); return; }
  const form = new FormData();
  form.append("file", f);
  const url = `${API}/suppliers/${orgId}/contracts` + (kind ? `?kind=${encodeURIComponent(kind)}` : "");
  try {
    const res = await fetch(url, { method: "POST", headers: { Authorization: `Bearer ${token}` }, body: form });
    if (!res.ok) {
      let d = `Upload failed (${res.status})`;
      try { d = (await res.json()).detail || d; } catch (e) {}
      toast(d, "err"); return;
    }
    toast("Contract uploaded", "ok");
    loadContractFiles(orgId);
  } catch (e) { toast("Upload failed", "err"); }
}

async function downloadContractFile(orgId, docId, filename) {
  try {
    const res = await fetch(`${API}/suppliers/${orgId}/contracts/${docId}/download`,
      { headers: { Authorization: `Bearer ${token}` } });
    if (!res.ok) { toast("Download failed", "err"); return; }
    const blob = await res.blob();
    const u = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = u; a.download = filename || "contract.pdf";
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(u);
  } catch (e) { toast("Download failed", "err"); }
}

async function removeContractFile(orgId, docId) {
  try {
    await api(`/suppliers/${orgId}/contracts/${docId}`, { method: "DELETE" });
    toast("Contract removed", "ok");
    loadContractFiles(orgId);
  } catch (e) { toast("Couldn't remove contract", "err"); }
}

/* ── Autonomy label (used by the Tweaks panel) ─────────────────────────
   The old top-bar "✨ Agent" drawer (weekly purchasing-run preview + insights)
   was removed — the Requisitions page is the single, working surface for the
   agent's staged buys. Only the autonomy-label helper remains, since the Tweaks
   panel still shows the current autonomy setting. */
const AUTONOMY = { suggest: "Suggest only", low: "Auto-close low-risk", full: "Full autonomy" };

function autonomyLabel() { return AUTONOMY[(readTweaks().autonomy)] || AUTONOMY.low; }

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
window.addEventListener("message", (e) => {
  const ty = e.data && e.data.type;
  if (ty === "__activate_edit_mode") openTweaks();
  else if (ty === "__deactivate_edit_mode") closeTweaks();
});
try { parent.postMessage({ type: "__edit_mode_available" }, "*"); } catch (e) {}
