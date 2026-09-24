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
const whSig = (W) => ((W && W.compartments) || []).map((c) => `${c.code}:${c.on_hand}:${c.verdict}:${c.median_days}`).join("|");
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

/* ── what is inside one compartment ──────────────────────────────────
   The owner, looking at the bar chart: "we need to be able to click on each
   individually and know what is inside, which items and how many." One call to
   /warehouse/compartments/{code}/contents answers, in the order a person asks:
   which devices (by model, by class), how old against the target (three bands),
   what condition (grades, first against second life, battery), the oldest
   serials to move first, and what is on its way in (the open order lines, which
   is the inbound band of the cockpit's bar). Every share comes from the payload;
   nothing is computed here beyond formatting. A block the data cannot support
   shows its reason, never an empty chart. */
const WH_BAND_TONE = { within_target: "var(--ts-positive)", past_target: "var(--ts-warning)", far_past_target: "var(--ts-negative)" };
const WH_GRADE_TONE = { A: "var(--ts-positive)", B: "var(--ts-info)", C: "var(--ts-warning)", D: "var(--ts-negative)" };
const WH_CYCLE_TONE = { "0": "var(--ts-brand-gold)", "1": "var(--ts-info)", "2+": "var(--ts-ink-soft)" };
const whShare = (v) => v == null ? "—" : (v * 100).toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " %";

/* a stacked bar plus one legend row per segment: {label, units, share, tone, hint} */
function whMix(segs) {
  const bar = `<div class="wh-mix">${segs.filter((s) => s.share > 0).map((s) => `<div class="wh-mix__seg" style="width:${s.share * 100}%;background:${s.tone}" title="${esc(s.label)}: ${num(s.units)} (${whShare(s.share)})"></div>`).join("")}</div>`;
  const rows = segs.map((s) => `<div class="wh-mix__row"><span class="wh-mix__dot" style="background:${s.tone}"></span><span class="wh-mix__k">${esc(s.label)}${s.hint ? ` <span class="muted">${esc(s.hint)}</span>` : ""}</span><span class="wh-mix__v">${num(s.units)}</span><span class="wh-mix__s">${whShare(s.share)}</span></div>`).join("");
  return bar + rows;
}

const whTile = (label, val, hint, hintCls = "") =>
  `<div class="wh-cnt__tile"><div class="wh-cnt__tile-label">${esc(label)}</div><div class="wh-cnt__tile-val">${val}</div>${hint ? `<div class="wh-note ${hintCls}">${hint}</div>` : ""}</div>`;

function whContentsHtml(d) {
  const a = d.age, c = d.condition, i = d.inbound;
  const models = d.by_model.length, classes = d.by_class.length;

  // the strip: the four questions, one number each
  const full = d.capacity == null
    ? whTile("Inside", `${num(d.on_hand)}<small>devices</small>`, esc(d.capacity_reason || ""))
    : whTile("Inside", `${num(d.on_hand)}<small>of ${num(d.capacity)} · ${whShare(d.utilisation)}</small>`,
        `${num(models)} model${models === 1 ? "" : "s"} in ${num(classes)} class${classes === 1 ? "" : "es"}${d.over_capacity ? ` · ${num(d.overflow)} over capacity` : ""}`, d.over_capacity ? "wh-note--warn" : "");
  const age = a
    ? whTile(`Past the target of ${d.target_dwell_days} d`, `${num(a.past_target_units)}<small>${whShare(a.past_target_share)}</small>`,
        `${num(a.far_past_units)} (${whShare(a.far_past_share)}) more than twice as long, over ${a.far_past_from_days - 1} d · median ${whDays(a.median_days)}`, a.far_past_share > 0.25 ? "wh-note--warn" : "")
    : whTile(`Past the target of ${d.target_dwell_days} d`, `<span class="wh-na">n/a</span>`, esc(d.age_reason || ""));
  // grades A and B are the ones a second rental takes; C and D are repair or sale
  const grade = (g) => (c && c.grades ? c.grades.find((x) => x.grade === g) : null);
  const goodShare = c && c.grades ? (grade("A") ? grade("A").share : 0) + (grade("B") ? grade("B").share : 0) : null;
  const battery = c && c.battery_health_mean != null ? ` · battery ${whShare(c.battery_health_mean)} mean` : "";
  const cond = c
    ? (c.grades
        ? whTile("Condition", `${whShare(goodShare)}<small>grade A or B</small>`,
            `${c.grades.filter((g) => g.grade !== "A" && g.grade !== "B").map((g) => `${esc(g.grade)} ${whShare(g.share)}`).join(" · ") || "no C or D"}${c.ungraded_units ? ` · ${num(c.ungraded_units)} not yet graded` : ""}${battery}`)
        : whTile("Condition", `<span class="wh-na">not graded</span>`, `${esc(c.grade_reason || "")}${battery}`))
    : whTile("Condition", `<span class="wh-na">n/a</span>`, esc(d.condition_reason || ""));
  const inb = i.units == null
    ? whTile("On its way in", `<span class="wh-na">n/a</span>`, esc(i.reason || ""))
    : i.units === 0
      ? whTile("On its way in", `0<small>devices</small>`, esc(i.reason || ""))
      : whTile("On its way in", `${num(i.units)}<small>on ${num(i.lines.length)} line${i.lines.length === 1 ? "" : "s"}</small>`,
          `${i.late_units ? `<b>${num(i.late_units)} late</b> on ${num(i.late_lines)} line${i.late_lines === 1 ? "" : "s"} · ` : ""}with them ${whShare(i.committed_share)} committed${i.committed_share > 1 ? `, ${num(i.committed - d.capacity)} over` : ""}`,
          i.late_units || i.committed_share > 1 ? "wh-note--warn" : "");

  // which devices
  const modelRows = d.by_model.map((m) => `<tr>
      <td class="wide"><div class="cell-prod__name">${esc(m.name)}</div><div class="cell-prod__cat">${esc(m.family || "no device class")}</div></td>
      <td class="num" style="font-weight:600">${num(m.units)}</td>
      <td class="num"><span class="wh-share"><span class="wh-share__fill" style="width:${Math.min(1, m.share || 0) * 100}%;display:block"></span></span>${whShare(m.share)}</td>
      <td class="num${m.past_target_units ? "" : " muted"}">${num(m.past_target_units)}</td>
      <td class="num${m.far_past_units ? "" : " muted"}">${num(m.far_past_units)}</td>
      <td class="num muted">${m.oldest_days == null ? "—" : num(m.oldest_days) + " d"}</td>
    </tr>`).join("");
  const byModel = `<div class="wh-cnt__block"><div class="wh-detail__head">Which devices · ${num(d.on_hand)} in ${num(models)} model${models === 1 ? "" : "s"}</div>
    ${d.by_class.length ? `<div class="wh-note" style="margin:0 0 8px">${d.by_class.map((k) => `<b>${esc(k.label)}</b> ${num(k.units)} (${whShare(k.share)})`).join(" · ")}</div>` : ""}
    ${models ? `<table class="tbl wh-models"><thead><tr><th>Model · class</th><th class="num">Units</th><th class="num">Share</th><th class="num">Past ${d.target_dwell_days} d</th><th class="num">Past ${a ? a.far_past_from_days - 1 : d.target_dwell_days * 2} d</th><th class="num">Oldest</th></tr></thead><tbody>${modelRows}</tbody></table>`
             : `<div class="muted">Nothing in this compartment.</div>`}
    ${d.undated_units ? `<div class="wh-note">${num(d.undated_units)} unit${d.undated_units === 1 ? "" : "s"} without a dwell date: counted, never aged.</div>` : ""}</div>`;

  // how old
  const howOld = `<div class="wh-cnt__block"><div class="wh-detail__head">How old · target ${d.target_dwell_days} d (${esc(d.target_owner)}, placeholder)</div>
    ${a ? whMix(a.bands.map((b) => ({ label: b.label, hint: b.to_days == null ? `over ${b.from_days - 1} d` : `${b.from_days} to ${b.to_days} d`, units: b.units, share: b.share, tone: WH_BAND_TONE[b.key] })))
        + `<div class="wh-note">median ${whDays(a.median_days)} · p90 ${whDays(a.p90_days)} · mean ${whDays(a.mean_days)} · oldest ${whDays(a.oldest_days)}${a.undated_units ? ` · ${num(a.undated_units)} undated` : ""}</div>`
        : `<div class="muted">${esc(d.age_reason || "")}</div>`}</div>`;

  // what condition
  const condition = `<div class="wh-cnt__block"><div class="wh-detail__head">What condition</div>
    ${c ? `${c.grades ? whMix(c.grades.map((g) => ({ label: g.label, units: g.units, share: g.share, tone: WH_GRADE_TONE[g.grade] || "var(--ts-line-strong)" }))
                             .concat(c.ungraded_units ? [{ label: "Not yet graded", units: c.ungraded_units, share: c.ungraded_share, tone: "var(--ts-line-strong)" }] : []))
                      : `<div class="wh-note" style="margin:0 0 8px">${esc(c.grade_reason || "")}</div>`}
           <div style="margin-top:10px">${whMix(c.cycles.map((x) => ({ label: x.label, units: x.units, share: x.share, tone: WH_CYCLE_TONE[x.cycle] || "var(--ts-line-strong)" })))}</div>
           <div class="wh-note">${c.battery_health_mean != null ? `battery health ${whShare(c.battery_health_mean)} mean over ${num(c.battery_health_units)} read` : esc(c.battery_reason || "")}</div>`
        : `<div class="muted">${esc(d.condition_reason || "")}</div>`}</div>`;

  // on its way in
  let inbound = "";
  if (i.units != null && i.lines.length) {
    const lineRows = i.lines.map((r) => `<tr>
        <td class="nowrap"><span class="ref">${esc(r.order_number)}</span></td>
        <td class="wide"><div class="cell-prod__name">${esc(r.product)}</div></td>
        <td class="num" style="font-weight:600">${num(r.outstanding)}${r.received ? `<div class="wh-note" style="margin:0">${num(r.received)} of ${num(r.ordered)} in</div>` : ""}</td>
        <td class="num">${r.eta ? fmtDate(r.eta) : `<span class="wh-na" title="${esc(r.eta_reason || "")}">no ETA</span>`}</td>
        <td>${r.late == null ? `<span class="muted">—</span>` : r.late ? plainPill(`${num(r.days_late)} d late`, "negative") : plainPill(`in ${num(r.days_to_eta)} d`, "neutral")}</td>
      </tr>`).join("");
    inbound = `<div class="wh-cnt__block"><div class="wh-detail__head">On its way in · ${num(i.units)} devices on ${num(i.lines.length)} open line${i.lines.length === 1 ? "" : "s"}</div>
      <div class="wh-note" style="margin:0 0 8px">${d.capacity != null ? `${num(d.on_hand)} on hand + ${num(i.units)} inbound = ${num(i.committed)} committed, ${whShare(i.committed_share)} of ${num(d.capacity)}` : `${num(d.on_hand)} on hand + ${num(i.units)} inbound = ${num(i.committed)} committed`}${i.late_units ? ` · <b>${num(i.late_units)} late on ${num(i.late_lines)} line${i.late_lines === 1 ? "" : "s"}</b>` : ""} · next delivery ${fmtDate(i.next_eta)}, last ${fmtDate(i.last_eta)}</div>
      <table class="tbl wh-lines"><thead><tr><th>Order</th><th>Model</th><th class="num">Outstanding</th><th class="num">ETA</th><th>Due</th></tr></thead><tbody>${lineRows}</tbody></table>
      <div class="wh-note" title="${esc(i.basis)}">Open lines destined for station ${esc(i.station)}: outstanding is ordered minus received; late is an ETA before today.</div></div>`;
  }

  // the oldest serials
  const oldest = `<div class="wh-cnt__block"><div class="wh-detail__head">Oldest units, the ones to move first</div>
    ${d.oldest.length ? `<table class="tbl wh-oldest"><thead><tr><th>Serial</th><th>Device</th><th class="num">Grade</th><th class="num">Rentals</th><th class="num">Days here</th></tr></thead>
        <tbody>${d.oldest.map((r) => `<tr><td class="nowrap"><span class="ref">${esc(r.serial_number)}</span></td><td class="wide">${esc(r.product)}</td><td class="num">${esc(r.grade || "—")}</td><td class="num muted">${whRental(r.cycle_no)}</td><td class="num" style="font-weight:600">${num(r.days)}</td></tr>`).join("")}</tbody></table>`
                     : `<div class="muted">No dated unit in this compartment.</div>`}</div>`;

  // which devices beside how old and what condition; the two wide tables (open lines, oldest serials) below at full width
  return `<div class="wh-cnt">
    <div class="wh-cnt__strip">${full}${age}${cond}${inb}</div>
    <div class="wh-cnt__grid">
      <div class="wh-cnt__col">${byModel}</div>
      <div class="wh-cnt__col">${howOld}${condition}</div>
    </div>
    ${inbound}${oldest}</div>`;
}

async function whLoadDetail(code, host) {
  try {
    const d = await api(`/warehouse/compartments/${encodeURIComponent(code)}/contents`);
    host.innerHTML = whContentsHtml(d);
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
        <div class="section__head"><span class="section__title">Compartments</span><span class="section__count">${C.length}</span><span class="section__hint">targets are placeholders until the named owner sets them · open a row to see what is inside · read ${new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" })}, refreshed while this tab is open</span></div>
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
    // Live: a fleet event on the Simulation tab moves stock between compartments; this tab
    // redraws when a count, a verdict or a dwell changed. One grouped read every 15 seconds,
    // the same read as the page itself, a quarter of a second on the full fleet.
    livePoll("warehouse", async () => {
      const W2 = await api("/warehouse/compartments");
      if (whSig(W2) !== whSig(W)) RENDER.warehouse();
    }, 15000);
  } catch (e) { screen.innerHTML = errState(e.message); }
};
