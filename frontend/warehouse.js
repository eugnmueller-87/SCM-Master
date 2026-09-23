"use strict";
/* ============================================================================
   SCM Master — Warehouse (device-as-a-service). Loads after returns.js.

   The warehouse as the owner tracks it: one compartment per category of stock,
   never mixed. New stock, the return chain, second-life stock, sellable stock
   and the swap buffer each answer three questions: how full, how fast it
   rotates, how well it is doing. Every figure comes from /warehouse/compartments
   and is computed in the database; throughput is derived by Little's law from
   stock and dwell and is labelled as derived, never presented as measured.
   A compartment with no data says why instead of showing a zero.

   In the datacenter scenario the tab keeps the location capacity view (app.js).
============================================================================ */

CRUMBS.warehouse = "Warehouse";

const WH_VERDICT = {
  healthy:       { label: "Healthy",       tone: "positive" },
  slow_moving:   { label: "Slow moving",   tone: "warning" },
  over_capacity: { label: "Over capacity", tone: "negative" },
  stalled:       { label: "Stalled",       tone: "negative" },
  empty:         { label: "Empty",         tone: "mute" },
  unknown:       { label: "No dwell data", tone: "mute" },
};
const WH_STAGE = { "first life": "First life", "return chain": "Return chain", "second life": "Second life", "exit": "Exit", "reserve": "Reserve" };

const whDays = (v) => v == null ? "—" : Math.round(v).toLocaleString("de-DE") + " d";
const whPct = (v) => v == null ? "—" : Math.round(v * 100) + " %";
const whRental = (c) => c === 0 ? "new" : c === 1 ? "1 rental" : `${c} rentals`;

/* one card of the chain strip */
function whStep(c) {
  const v = WH_VERDICT[c.verdict] || WH_VERDICT.unknown;
  const t = TONE[v.tone];
  const u = c.utilisation == null ? 0 : Math.min(c.utilisation, 1);
  return `<div class="wh-step" data-code="${esc(c.code)}" title="${esc(c.holds)}">
    <div class="wh-step__stage">${esc(WH_STAGE[c.stage] || c.stage)}</div>
    <div class="wh-step__name">${esc(c.name)}</div>
    <div class="wh-step__n">${num(c.on_hand)}</div>
    <div class="wh-step__bar"><div class="wh-step__fill" style="width:${u * 100}%;background:${capTone(c.utilisation || 0, c.over_capacity)}"></div></div>
    <div class="wh-step__verdict" style="color:${t.fg}">${v.label}</div>
  </div>`;
}

/* one row of the table, plus its hidden detail row */
function whRow(c) {
  const v = WH_VERDICT[c.verdict] || WH_VERDICT.unknown;
  const u = c.utilisation;
  const tone = capTone(u || 0, c.over_capacity);
  const capCell = c.capacity == null
    ? `<div style="font-weight:600">${num(c.on_hand)}</div><div class="wh-note">${esc(c.capacity_reason || "no capacity")}</div>`
    : `<div style="display:flex;align-items:center;gap:12px;justify-content:flex-end"><div class="cap-bar"><div class="cap-bar__fill" style="width:${Math.min(u, 1) * 100}%;background:${tone}"></div></div><span class="cap-util" style="color:${tone}">${pct(u)}</span></div>
       <div class="wh-note">${num(c.on_hand)} of ${num(c.capacity)}${c.over_capacity ? ` · ${num(c.overflow)} over` : ` · ${num(c.free)} free`}</div>`;
  const dwell = c.median_days == null
    ? `<div class="wh-na">n/a</div><div class="wh-note">${esc(c.dwell_reason || "")}</div>`
    : `<div><b>${whDays(c.median_days)}</b> median · ${whDays(c.p90_days)} p90</div>
       <div class="wh-note${c.past_target_share > 0.25 ? " wh-note--warn" : ""}">${whPct(c.past_target_share)} past the target of ${c.target_dwell_days} d · oldest ${whDays(c.oldest_days)}</div>`;
  const flow = c.units_per_week == null
    ? `<div class="wh-na">n/a</div><div class="wh-note">${esc(c.throughput_reason || "")}</div>`
    : `<div><b>${num(Math.round(c.units_per_week))}</b> / week</div><div class="wh-note">${Number(c.turns_per_year).toLocaleString("de-DE", { maximumFractionDigits: 1 })} turns / year <span class="wh-derived" title="${esc(c.throughput_basis)}">derived</span></div>`;
  return `<tr class="clickable wh-row" data-code="${esc(c.code)}">
    <td><div class="cell-prod"><span class="cell-prod__icon">${icon("layers", 15)}</span><div><div class="cell-prod__name">${c.step}. ${esc(c.name)}</div><div class="cell-prod__cat">${esc(c.holds)}</div></div></div></td>
    <td class="muted">${esc(WH_STAGE[c.stage] || c.stage)}</td>
    <td class="num">${capCell}</td>
    <td>${dwell}</td>
    <td class="num">${flow}</td>
    <td>${plainPill(v.label, v.tone)}<div class="wh-note">${esc(c.verdict_reason || "")}</div></td>
  </tr>
  <tr class="wh-detail hidden" data-detail="${esc(c.code)}"><td colspan="6"><div class="wh-detail__inner" id="wh-detail-${esc(c.code)}"><span class="muted">Loading…</span></div></td></tr>`;
}

/* the worst offenders of one compartment: the late stock by device, and the oldest serials */
async function whLoadDetail(code, host) {
  try {
    const d = await api(`/warehouse/compartments/${encodeURIComponent(code)}/offenders?limit=15`);
    const prods = d.past_target_by_product.length
      ? `<div class="wh-detail__col"><div class="wh-detail__head">Past the target of ${d.target_dwell_days} d, by device</div>
          ${d.past_target_by_product.map((p) => `<div class="prov__row"><span class="prov__k">${esc(p.name)}<span class="muted" style="margin-left:6px;font-size:11px">${esc(p.family || "")}</span></span><span class="prov__v" style="font-weight:600;font-variant-numeric:tabular-nums">${num(p.units)}</span></div>`).join("")}</div>`
      : `<div class="wh-detail__col"><div class="wh-detail__head">Past the target of ${d.target_dwell_days} d</div><div class="muted">Nothing past target.</div></div>`;
    const oldest = d.oldest.length
      ? `<table class="tbl wh-oldest"><thead><tr><th>Serial</th><th>Device</th><th class="num">Grade</th><th class="num">Rentals</th><th class="num">Days here</th></tr></thead>
          <tbody>${d.oldest.map((r) => `<tr><td><span class="ref">${esc(r.serial_number)}</span></td><td>${esc(r.product)}</td><td class="num">${esc(r.grade || "—")}</td><td class="num muted">${whRental(r.cycle_no)}</td><td class="num" style="font-weight:600">${num(r.days)}</td></tr>`).join("")}</tbody></table>`
      : `<div class="muted">No dated unit in this compartment.</div>`;
    host.innerHTML = `<div class="wh-detail__grid">${prods}<div class="wh-detail__col"><div class="wh-detail__head">Oldest units, the ones to move first</div>${oldest}</div></div>`;
  } catch (e) {
    host.innerHTML = `<span class="muted">${esc((e && e.message) || "Could not load")}</span>`;
  }
}

RENDER.warehouse = async function () {
  // The datacenter operation keeps its location capacity view; compartments are a fleet thing.
  if (!isDaas()) return RENDER.capacity();
  const screen = $("#screen");
  try {
    const W = await api("/warehouse/compartments");
    const C = W.compartments || [];
    const count = (v) => C.filter((c) => c.verdict === v).length;
    const second = C.find((c) => c.code === "ST-SECOND") || {};
    const stat = (label, ic, val, hint, hintCls = "", valCls = "") =>
      `<div class="stat"><div class="stat__label">${icon(ic, 14)} ${label}</div><div class="stat__val ${valCls}">${val}</div>${hint ? `<div class="stat__hint ${hintCls}">${hint}</div>` : ""}</div>`;
    const chain = C.map(whStep).join(`<div class="wh-step__arrow">${icon("arrow", 14)}</div>`);
    const stalled = count("stalled"), slow = count("slow_moving");

    screen.innerHTML = `
      ${pageHead("Warehouse", "Compartments", "One compartment per category of stock, never mixed: new stock, the return chain, second-life stock, sellable stock and the swap buffer. For each: how full, how fast it rotates, how well it is doing. Computed in the database from every serial; flow and turns are derived from stock and dwell and say so.")}
      <div class="stats stats--5">
        ${stat("On hand", "layers", num(W.on_hand), `${num(C.length)} compartments`)}
        ${stat("Capacity", "box", W.capacity == null ? "—" : num(W.capacity), W.capacity == null ? esc(W.reason || "no capacity defined") : `${whPct(W.utilisation)} used`)}
        ${stat("Free", "check", W.free == null ? "—" : num(W.free), W.over_capacity ? `${W.over_capacity} compartment${W.over_capacity > 1 ? "s" : ""} over capacity` : "no compartment over capacity", W.over_capacity ? "stat__hint--neg" : "stat__hint--pos")}
        ${stat("Second-life stock", "return", num(second.on_hand), second.median_days != null ? `refurbished, waiting ${whDays(second.median_days)} median for a second customer` : "refurbished, waiting for a second customer", "", "stat__val--gold")}
        ${stat("Stalled or slow", "alert", num(stalled + slow), `${num(stalled)} stalled · ${num(slow)} slow moving`, stalled ? "stat__hint--neg" : "")}
      </div>
      <div class="section">
        <div class="section__head"><span class="section__title">The chain</span><span class="section__count">${C.length}</span><span class="section__hint">new stock → rented → return chain → second life or exit · the swap buffer stands aside</span></div>
        <div class="panel wh-chain">${chain}</div>
      </div>
      <div class="section" style="margin-bottom:0">
        <div class="section__head"><span class="section__title">Compartments</span><span class="section__count">${C.length}</span><span class="section__hint">targets are placeholders until the named owner sets them · open a row for the worst offenders</span></div>
        <div class="panel"><table class="tbl wh-tbl">
          <thead><tr><th>Compartment</th><th>Stage</th><th class="num" style="width:220px">How full</th><th>How fast · dwell</th><th class="num">Flow, derived</th><th>How well</th></tr></thead>
          <tbody>${C.map(whRow).join("") || `<tr><td colspan="6"><div class="state"><div class="state__sub">${esc(W.reason || "No compartments.")}</div></div></td></tr>`}</tbody>
        </table></div>
        <div class="wh-foot">Target dwell per compartment: ${C.map((c) => `${esc(c.name)} ${c.target_dwell_days} d (${esc(c.target_owner)})`).join(" · ")}. Placeholders, the owning role in brackets. Flow and turns are derived by Little's law from stock and the mean dwell of the stock still here, an upper bound; a measured flow needs the movement log.</div>
      </div>`;

    const open = (code) => {
      const detail = $(`#screen [data-detail="${code}"]`);
      if (!detail) return;
      const wasHidden = detail.classList.contains("hidden");
      detail.classList.toggle("hidden");
      const host = $(`#wh-detail-${code}`);
      if (wasHidden && host && !host.dataset.loaded) { host.dataset.loaded = "1"; whLoadDetail(code, host); }
    };
    $$("#screen .wh-row").forEach((r) => r.addEventListener("click", () => open(r.dataset.code)));
    $$("#screen .wh-step").forEach((s) => s.addEventListener("click", () => {
      const row = $(`#screen .wh-row[data-code="${s.dataset.code}"]`);
      if (row) { row.scrollIntoView({ behavior: "smooth", block: "center" }); if ($(`#screen [data-detail="${s.dataset.code}"]`).classList.contains("hidden")) open(s.dataset.code); }
    }));
  } catch (e) { screen.innerHTML = errState(e.message); }
};
