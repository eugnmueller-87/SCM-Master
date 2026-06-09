"use strict";
/* ============================================================================
   SCM Master — Requisitions (the agent's cart). Loads after inventory.js.

   The PR → PO workflow made visible:
     • "Run agent" detects demand and stages Purchase Requisitions (PRs).
     • A PR whose confidence clears its *calibrated* bar auto-converts to a PO
       (shown in the Auto-placed strip — reversible until received).
     • Everything below the bar lands here as an editable cart: adjust line
       quantities, drop lines, then Approve (→ becomes a fixed PO) or Reject.
     • Each decision teaches the agent (see the "What the agent has learned"
       panel): trusted product/supplier pairs earn a lower bar over time.

   A PR is editable; a PO is not. That distinction is the whole point.
============================================================================ */

ICONS.cart = '<path d="M3 3h2l2.4 12.3a1 1 0 0 0 1 .8h9.2a1 1 0 0 0 1-.8L21 7H6"/><circle cx="9" cy="20" r="1.5"/><circle cx="18" cy="20" r="1.5"/>';

CRUMBS.requisitions = "Requisitions";
if (typeof COUNTS === "object") COUNTS.staged = null;

const TIER_TONE = { act: "positive", propose: "warning", escalate: "negative" };
const supName = (id) => (ORGS[id] ? ORGS[id].name : (id || "—").slice(0, 8));
// `pct` is provided globally by app.js — do NOT redeclare it here. A second
// top-level `const pct` is a SyntaxError that aborts parsing this whole file,
// which silently breaks the Requisitions route (blank "Loading…").

/* ── data ──────────────────────────────────────────────────────────── */
async function loadReqs() {
  const [staged, placed, calib, position] = await Promise.all([
    api("/requisitions?status=STAGED").catch(() => []),
    api("/requisitions?status=PLACED&limit=20").catch(() => []),
    api("/requisitions/calibration").catch(() => []),
    api("/planning/inventory-position").catch(() => []),
  ]);
  return { staged: staged || [], placed: placed || [], calib: calib || [], position: position || [] };
}

/* ── render ────────────────────────────────────────────────────────── */
let _POSROWS = [];   // inventory-position rows, for the panel drill-down
RENDER.requisitions = async function () {
  const { staged, placed, calib, position } = await loadReqs();
  _POSROWS = position;   // for the position-panel drill-down
  if (typeof COUNTS === "object") { COUNTS.staged = staged.length; renderNav(); }

  const autoPlaced = placed.filter((p) => p.auto_placed);

  const head = pageHead(
    "Automation", "Requisitions",
    "The agent stages purchase requests from live demand. High-confidence ones auto-place; the rest wait here for your approval — adjustable, like a cart. A PR is editable; once approved it becomes a fixed PO.",
    `<button class="btn btn--ink" id="req-run">${icon("gauge", 15)} Run agent</button>`
  );

  const kpis = `<div class="tkpis">
    <div class="tkpi"><div class="tkpi__label">Awaiting approval</div><div class="tkpi__val${staged.length ? " tkpi__val--neg" : ""}">${staged.length}</div></div>
    <div class="tkpi"><div class="tkpi__label">Auto-placed (recent)</div><div class="tkpi__val tkpi__val--info">${autoPlaced.length}</div></div>
    <div class="tkpi"><div class="tkpi__label">Auto-place bar</div><div class="tkpi__val">85%<span style="font-size:12px;color:var(--ts-ink-faint)"> default</span></div></div>
    <div class="tkpi"><div class="tkpi__label">Learned pairs</div><div class="tkpi__val">${calib.length}</div></div>
  </div>`;

  const autoStrip = autoPlaced.length ? `<div class="panel" style="padding:12px 16px;margin-bottom:14px;border-left:3px solid var(--ts-positive)">
    <div style="font-weight:600;margin-bottom:6px">${icon("check", 14)} Auto-placed — confidence cleared the bar (reversible until received)</div>
    <div style="display:flex;flex-wrap:wrap;gap:8px">
      ${autoPlaced.map((p) => `<span class="plain-pill" style="background:var(--ts-positive-bg,#e7f4ec);color:var(--ts-positive)">${esc(supName(p.supplier_id))} · ${p.lines.length} line(s) · conf ${pct(p.confidence)}</span>`).join("")}
    </div></div>` : "";

  const cards = staged.length
    ? staged.map(reqCard).join("")
    : `<div class="panel" style="padding:28px;text-align:center;color:var(--ts-ink-mute)">
         No requisitions awaiting approval. Click <strong>Run agent</strong> to detect demand and stage proposals.</div>`;

  $("#screen").innerHTML = `<div class="fade-in">
    ${head}${kpis}
    ${positionPanel(position)}
    ${autoStrip}
    <div id="req-cards">${cards}</div>
    ${calibPanel(calib)}
  </div>`;

  const runBtn = $("#req-run");
  if (runBtn) runBtn.addEventListener("click", () => runAgent(runBtn));
  wirePositionDrill();
  wireCards();
};

/* ── Inventory-position panel — THE instrument ─────────────────────────
   One row per product, every column from /planning/inventory-position (the
   same canonical model the agent nets against — they cannot disagree). The
   Missing column splits visibly into Proposing (orderable now) vs Deferred
   (capacity-blocked), which is what makes the residual loop self-explanatory.
   Row click expands the on_hand/on_order breakdown + the PO numbers. */
function positionPanel(rows) {
  const active = (rows || []).filter((r) => r.net_requirement > 0 || r.on_order > 0 || r.staged_planned > 0 || r.deferred > 0 || r.at_risk);
  if (!active.length) {
    return `<div class="panel ipos" style="padding:20px;color:var(--ts-ink-mute)">Inventory position: every product is covered — nothing missing, nothing deferred.</div>`;
  }
  const t = active.reduce((a, r) => ({
    missing: a.missing + r.net_requirement, staged: a.staged + r.staged_planned,
    deferred: a.deferred + r.deferred, committed: a.committed + (r.committed_value || 0),
    atrisk: a.atrisk + (r.at_risk ? 1 : 0),
  }), { missing: 0, staged: 0, deferred: 0, committed: 0, atrisk: 0 });

  const strip = `<div class="ipos-strip">
    <div class="ipos-kpi"><div class="ipos-kpi__lbl">Still missing</div><div class="ipos-kpi__val">${t.missing}<span class="ipos-kpi__u"> units</span></div></div>
    <div class="ipos-kpi"><div class="ipos-kpi__lbl">Staged now</div><div class="ipos-kpi__val ipos-prop">${t.staged}<span class="ipos-kpi__u"> units queued</span></div></div>
    <div class="ipos-kpi"><div class="ipos-kpi__lbl">Capacity-deferred</div><div class="ipos-kpi__val ${t.deferred ? "ipos-defer" : ""}">${t.deferred}<span class="ipos-kpi__u"> blocked</span></div></div>
    <div class="ipos-kpi"><div class="ipos-kpi__lbl">At risk · runs dry first</div><div class="ipos-kpi__val ${t.atrisk ? "ipos-risk" : ""}">${t.atrisk}<span class="ipos-kpi__u"> SKUs</span></div></div>
  </div>`;

  const cov = (r) => {
    if (r.cover_days == null) return "—";
    const c = r.cover_days >= 365 ? "365+" : r.cover_days;
    const risk = r.at_risk ? ` <span class="ipos-flag-risk" title="Runs dry before the inbound order lands">⚠ dry first</span>` : "";
    return `${c}d${r.lands_in_days != null ? ` <span class="ipos-cov-vs">vs ${r.lands_in_days}d to land</span>` : ""}${risk}`;
  };

  const body = active.map((r) => {
    const capPct = (r.position + r.capacity_avail) > 0 ? Math.min(100, Math.round(r.position / (r.position + r.capacity_avail) * 100)) : 0;
    const stagedCell = r.staged_planned ? `<span class="ipos-pill ipos-pill--prop">${r.staged_planned}</span>` : "—";
    const deferCell = r.deferred ? `<span class="ipos-pill ipos-pill--defer">${r.deferred}</span>` : "—";
    return `<tr class="ipos-row${r.at_risk ? " ipos-row--risk" : ""}" data-ipid="${esc(r.product_id)}">
        <td><div class="ipos-name">${esc(r.name || r.product_id)}</div><div class="ipos-cat">${esc(r.category || "")}</div></td>
        <td class="num">${r.gross_demand}</td>
        <td class="num">${r.position}</td>
        <td class="num">${r.net_requirement || "—"}</td>
        <td class="num">${stagedCell}</td>
        <td class="num">${deferCell}</td>
        <td class="ipos-cov-cell">${cov(r)}</td>
        <td><div class="ipos-cap"><span class="ipos-cap__txt">${r.position} / ${r.position + r.capacity_avail}</span>
          <span class="ipos-cap__bar"><span style="width:${capPct}%;background:${r.deferred ? "var(--ts-warning)" : "var(--ts-positive)"}"></span></span></div></td>
        <td class="num">${eur0(r.committed_value)}</td>
      </tr>
      <tr class="ipos-drill" data-drill="${esc(r.product_id)}" hidden><td colspan="9"></td></tr>`;
  }).join("");

  return `<div class="panel ipos">
    <table class="ipos-tbl">
      <thead><tr><th>Item</th><th class="num">Need</th><th class="num">Position</th><th class="num">Missing</th>
        <th class="num">Staged now</th><th class="num">Deferred</th><th>Cover vs lead</th><th>Capacity</th><th class="num">Committed €</th></tr></thead>
      <tbody>${body}</tbody>
      <tfoot><tr>
        <td>Total</td><td class="num">${active.reduce((s, r) => s + r.gross_demand, 0)}</td>
        <td class="num">${active.reduce((s, r) => s + r.position, 0)}</td>
        <td class="num">${t.missing}</td>
        <td class="num ipos-prop">${t.staged}</td>
        <td class="num ${t.deferred ? "ipos-defer" : ""}">${t.deferred || "—"}</td>
        <td class="ipos-cap__txt">${t.atrisk} at risk</td>
        <td class="ipos-cap__txt">${active.filter((r) => r.deferred).length} at cap</td>
        <td class="num">${eur0(t.committed)}</td>
      </tr></tfoot>
    </table>
  </div>${strip}`;
}

// Store rows for drill-down lookup without re-fetching (declared near render).
function wirePositionDrill() {
  $$(".ipos-row").forEach((tr) => tr.addEventListener("click", () => {
    const pid = tr.dataset.ipid;
    const drill = $(`.ipos-drill[data-drill="${pid}"]`);
    if (!drill) return;
    const open = !drill.hasAttribute("hidden");
    $$(".ipos-drill").forEach((d) => d.setAttribute("hidden", ""));
    $$(".ipos-row").forEach((r) => r.classList.remove("ipos-row--open"));
    if (open) return;
    drill.removeAttribute("hidden");
    tr.classList.add("ipos-row--open");
    const r = _POSROWS.find((x) => x.product_id === pid);
    drill.querySelector("td").innerHTML = posDrill(r);
  }));
}
function posDrill(r) {
  if (!r) return "";
  const pos = `<div class="ipos-dl"><span>On hand</span><b>${r.on_hand}</b></div>
    <div class="ipos-dl"><span>On order (committed POs)</span><b>${r.on_order}</b></div>
    <div class="ipos-dl"><span>Staged (planned)</span><b>${r.staged_planned}</b></div>
    <div class="ipos-dl"><span>Safety stock</span><b>${r.safety_stock}</b></div>`;
  const pos2 = `<div class="ipos-eq">Need ${r.gross_demand} − Position ${r.position} − Safety ${r.safety_stock} = <b>Missing ${r.net_requirement}</b>` +
    (r.staged_planned ? ` · less ${r.staged_planned} staged → propose ${r.new_proposal}` : "") +
    (r.deferred ? ` · ${r.proposing} fit / ${r.deferred} deferred (capacity)` : "") + `</div>`;
  const pos3 = (r.po_lines && r.po_lines.length)
    ? `<table class="ipos-po"><thead><tr><th>PO</th><th class="num">Ordered</th><th class="num">Received</th><th class="num">Outstanding</th><th class="num">Unit</th><th>ETA</th></tr></thead><tbody>${
        r.po_lines.map((l) => `<tr><td>${esc(l.order_number || "—")}</td><td class="num">${l.ordered}</td><td class="num">${l.received}</td><td class="num">${l.outstanding}</td><td class="num">${l.unit_price != null ? eur0(l.unit_price) : "—"}</td><td>${l.eta ? shortDate(l.eta) : "—"}</td></tr>`).join("")
      }</tbody></table>`
    : `<div class="ipos-dl" style="color:var(--ts-ink-mute)">No open POs behind on_order.</div>`;
  return `<div class="ipos-drill__wrap"><div class="ipos-dl-grid">${pos}</div>${pos2}<div class="ipos-po-h">Open purchase orders behind on_order</div>${pos3}</div>`;
}
function eur0(n) { return "€" + Math.round(Number(n) || 0).toLocaleString("en-US"); }

/* ── one staged PR = one editable cart card ────────────────────────── */
/* Distinct flags on a requisition line: a RESIDUAL (the rest of a need already
   partly staged — net of the open PR, not a duplicate) and CAPACITY-CAPPED (the
   need exceeds warehouse headroom, so only what fits was staged). The backend
   threads these into the line rationale; we surface them so a follow-on proposal
   never reads as "ordering the same thing again". */
function reqLineFlags(l) {
  const r = l.rationale || "";
  let out = "";
  const resid = r.match(/residual: \+(\d+) beyond (\d+) already committed/);
  if (resid) {
    out += `<div class="req-flag req-flag--residual" title="Net of what's already on order or staged — the remainder, not a duplicate">`
      + `RESIDUAL +${resid[1]} · ${resid[2]} already on order/staged</div>`;
  }
  if (/capped to fit warehouse storage/.test(r)) {
    out += `<div class="req-flag req-flag--capped" title="The full need exceeds warehouse headroom — only what fits was staged">`
      + `CAPACITY-CAPPED</div>`;
  }
  return out;
}

function reqCard(pr) {
  const tone = TIER_TONE[pr.tier] || "info";
  const total = pr.lines.filter((l) => l.included).reduce((s, l) => s + (l.qty * (Number(l.unit_price) || 0)), 0);
  const cleared = pr.confidence >= pr.confidence_floor;
  const barNote = `confidence ${pct(pr.confidence)} vs bar ${pct(pr.confidence_floor)} — ${cleared ? "would auto-place" : "needs approval"}`;

  const lines = pr.lines.map((l) => {
    const p = PRODUCTS[l.product_id] || {};
    const edited = l.qty !== l.proposed_qty;
    return `<tr data-line="${l.id}" data-unit="${Number(l.unit_price) || 0}" class="${l.included ? "" : "req-line--dropped"}">
      <td><label class="req-incl"><input type="checkbox" class="req-incl-cb" ${l.included ? "checked" : ""}/> </label></td>
      <td>${productCell(l.product_id)}</td>
      <td class="muted" style="font-size:12px">${esc(l.trigger_type || "")}${reqLineFlags(l)}</td>
      <td class="num"><input class="req-qty" type="number" min="1" value="${l.qty}" ${l.included ? "" : "disabled"} style="width:74px"/>
        ${edited ? `<div style="font-size:11px;color:var(--ts-warning)">was ${l.proposed_qty}</div>` : ""}</td>
      <td class="num muted">${l.unit_price != null ? "€" + Number(l.unit_price).toLocaleString() : "—"}</td>
      <td class="num">€${(l.qty * (Number(l.unit_price) || 0)).toLocaleString()}</td>
    </tr>`;
  }).join("");

  return `<div class="panel req-card" data-req="${pr.id}" style="margin-bottom:14px">
    <div class="req-card__head" style="display:flex;align-items:center;gap:10px;padding:12px 16px;border-bottom:1px solid var(--ts-line)">
      <strong>${esc(supName(pr.supplier_id))}</strong>
      ${plainPill(pr.tier.toUpperCase(), tone)}
      <span class="muted" style="font-size:12px">${esc(barNote)}</span>
      <span style="margin-left:auto;font-weight:600">€<span class="req-total">${total.toLocaleString()}</span></span>
    </div>
    <table class="tbl" style="margin:0">
      <thead><tr><th style="width:34px"></th><th>Item</th><th>Why</th><th class="num">Qty</th><th class="num">Unit</th><th class="num">Line</th></tr></thead>
      <tbody>${lines}</tbody>
    </table>
    <div style="display:flex;gap:10px;padding:12px 16px;align-items:center">
      ${pr.rationale ? `<span class="muted" style="font-size:12px;flex:1">${esc(pr.rationale.split("\n")[0])}</span>` : "<span style='flex:1'></span>"}
      <button class="btn btn--ghost req-reject">Reject</button>
      <button class="btn btn--ink req-approve">${icon("check", 14)} Approve → create PO</button>
    </div>
  </div>`;
}

function calibPanel(calib) {
  if (!calib.length) return `<div class="panel" style="padding:16px;margin-top:18px;color:var(--ts-ink-mute);font-size:13px">
    <strong>What the agent has learned</strong> — nothing yet. As you approve, edit, or reject requisitions, the agent adjusts the auto-place bar per product/supplier so trusted buys clear automatically.</div>`;
  const rows = calib.map((c) => {
    const moved = c.adjusted_floor - c.base_floor;
    const dir = moved < -0.001 ? `<span style="color:var(--ts-positive)">▼ ${pct(-moved)} lower</span>`
      : moved > 0.001 ? `<span style="color:var(--ts-negative)">▲ ${pct(moved)} higher</span>`
      : `<span class="muted">unchanged</span>`;
    return `<tr><td>${productCell(c.product_id, false)}</td><td class="muted">${esc(supName(c.supplier_id))}</td>
      <td class="num">${c.samples}</td><td class="num">${pct(c.approval_rate)}</td>
      <td class="num">${pct(c.adjusted_floor)}</td><td>${dir}</td>
      <td class="muted" style="font-size:12px">${esc(c.reason)}</td></tr>`;
  }).join("");
  return `<div class="panel" style="margin-top:18px">
    <div style="padding:12px 16px;border-bottom:1px solid var(--ts-line);font-weight:600">${icon("gauge", 15)} What the agent has learned — calibrated auto-place bars</div>
    <table class="tbl" style="margin:0"><thead><tr><th>Item</th><th>Supplier</th><th class="num">Samples</th><th class="num">Approval rate</th><th class="num">Auto-place bar</th><th>vs default</th><th>Why</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

/* ── interactions ──────────────────────────────────────────────────── */
function wireCards() {
  $$("#req-cards .req-card").forEach((card) => {
    const reqId = card.dataset.req;

    // recompute the card total as qty/include change (optimistic, local)
    const recompute = () => {
      let total = 0;
      card.querySelectorAll("tr[data-line]").forEach((tr) => {
        const cb = tr.querySelector(".req-incl-cb");
        const q = tr.querySelector(".req-qty");
        const unit = parseFloat(tr.dataset.unit) || 0;   // from data-unit, not the rendered text
        if (cb.checked) total += (parseInt(q.value, 10) || 0) * unit;
      });
      card.querySelector(".req-total").textContent = total.toLocaleString();
    };

    card.querySelectorAll("tr[data-line]").forEach((tr) => {
      const lineId = tr.dataset.line;
      const cb = tr.querySelector(".req-incl-cb");
      const q = tr.querySelector(".req-qty");
      cb.addEventListener("change", async () => {
        q.disabled = !cb.checked;
        tr.classList.toggle("req-line--dropped", !cb.checked);
        recompute();
        try { await api(`/requisitions/${reqId}/lines/${lineId}`, { method: "PATCH", body: { included: cb.checked } }); }
        catch (e) { toast(e.message, "err"); }
      });
      q.addEventListener("change", async () => {
        const val = parseInt(q.value, 10);
        if (!val || val < 1) { q.value = 1; }
        recompute();
        try { await api(`/requisitions/${reqId}/lines/${lineId}`, { method: "PATCH", body: { qty: parseInt(q.value, 10) } }); toast("Quantity updated"); }
        catch (e) { toast(e.message, "err"); }
      });
    });

    card.querySelector(".req-approve").addEventListener("click", async (e) => {
      const btn = e.currentTarget; btn.disabled = true;
      try {
        await api(`/requisitions/${reqId}/approve`, { method: "POST" });
        toast("Approved — purchase order created", "ok");
        RENDER.requisitions();
      } catch (err) { toast(err.message, "err"); btn.disabled = false; }
    });
    card.querySelector(".req-reject").addEventListener("click", async (e) => {
      const btn = e.currentTarget; btn.disabled = true;
      try {
        await api(`/requisitions/${reqId}/reject`, { method: "POST", body: { reason: "Dismissed by buyer" } });
        toast("Requisition rejected");
        RENDER.requisitions();
      } catch (err) { toast(err.message, "err"); btn.disabled = false; }
    });
  });
}

async function runAgent(btn) {
  btn.disabled = true; const label = btn.innerHTML; btn.innerHTML = "Running…";
  try {
    const res = await api("/requisitions/run", { method: "POST", body: { period_days: 7 } });
    const bits = [`${res.staged} staged`];
    if (res.auto_placed) bits.push(`${res.auto_placed} auto-placed`);
    if (res.escalations_no_source) bits.push(`${res.escalations_no_source} need a supplier`);
    toast(bits.join(" · "), "ok");
    RENDER.requisitions();
  } catch (e) {
    toast(e.message || "Run failed", "err");
    btn.disabled = false; btn.innerHTML = label;
  }
}
