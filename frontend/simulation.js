"use strict";
/* ============================================================================
   SCM Master — Simulation (device-as-a-service). Loads after tco.js.

   A test tab: the day-to-day of a device fleet, fired at the running system.
   Every event goes through the same service calls the console uses, so a move
   the state machine forbids is refused here too. Every answer says what moved
   between which compartments, what was refused and why, and which KPIs moved,
   before and after, measured again now. The tab writes to the demo database
   and says so; production refuses, and the read-only guest cannot fire.

   The event log lives in this page for this session only. The database is the
   record: every move is on the device's event log with the actor "simulation".
============================================================================ */

ICONS.play = '<path d="M7 5l12 7-12 7z"/>';
CRUMBS.simulation = "Simulation";

let SIM = { cat: null, status: null, log: [], sel: 0, busy: false };

const simCanFire = () => !!(SIM.status && SIM.status.can_fire);
const simCanReset = () => !!(SIM.status && SIM.status.can_reset);
const simTime = (iso) => iso ? new Date(iso).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
const simMs = (ms) => ms == null ? "—" : ms >= 1000 ? (ms / 1000).toLocaleString("de-DE", { maximumFractionDigits: 1 }) + " s" : ms + " ms";
const simSigned = (v, unit) => v == null ? "—" : (v > 0 ? "+" : v < 0 ? "−" : "") + kpiFmt(Math.abs(v), unit);
const simFlow = (v) => v == null ? `<span class="wh-na">n/a</span>` : `${num(v)} / day`;

/* the breach of the capacity plan, worded as the plan tab words it */
function simBreach(state, month) {
  if (!state) return `<span class="wh-na">—</span>`;
  const s = CP_STATE[state] || CP_STATE.unknown;
  const label = state === "later" ? cpMonth(month) : s.label;
  return `<span style="color:${TONE[s.tone].fg};font-weight:600">${esc(label)}</span>`;
}

/* ── the action cards ─────────────────────────────────────────────── */
function simParamField(a, p) {
  const id = `sim-${a.id}-${p.name}`;
  if (p.kind === "choice") {
    const opts = (SIM.cat.choices[p.choices] || []);
    const blank = p.choices === "products" ? `<option value="">any model</option>` : "";
    return `<div class="field"><label class="field__label" for="${id}">${esc(p.label)}</label>
      <select class="input" id="${id}" name="${p.name}">${blank}${opts.map((o) => `<option value="${esc(o.code)}"${o.code === p.default ? " selected" : ""}>${esc(o.name)}</option>`).join("")}</select></div>`;
  }
  const step = p.kind === "float" ? "0.05" : "1";
  const val = p.default == null ? "" : p.default;
  return `<div class="field"><label class="field__label" for="${id}">${esc(p.label)}</label>
    <input class="input" id="${id}" name="${p.name}" type="number" step="${step}"${p.min != null ? ` min="${p.min}"` : ""}${p.max != null ? ` max="${p.max}"` : ""} value="${val}" required /></div>`;
}

function simCard(a) {
  const hints = a.params.filter((p) => p.hint).map((p) => `${esc(p.label)}: ${esc(p.hint)}`).join(" · ");
  return `<form class="sim-card${a.id === "day" || a.id === "hold" ? " sim-card--key" : ""}" data-sim-action="${esc(a.id)}">
    <div class="sim-card__name">${esc(a.name)}</div>
    <div class="sim-card__desc">${esc(a.description)}</div>
    <div class="sim-form">${a.params.map((p) => simParamField(a, p)).join("")}
      <button type="submit" class="btn btn--ink btn--sm" ${simCanFire() ? "" : "disabled"} title="${simCanFire() ? "Fire this event at the running system" : "WAREHOUSE or ADMIN fires"}">${icon("play", 13)} Fire</button></div>
    ${hints ? `<div class="sim-form__hint">${hints}</div>` : ""}
  </form>`;
}

/* ── firing ───────────────────────────────────────────────────────── */
async function simFire(form) {
  if (SIM.busy) return;
  const id = form.dataset.simAction;
  const body = {};
  new FormData(form).forEach((v, k) => {
    if (v === "" || v == null) return;
    body[k] = (k === "station" || k === "product_code" || k.endsWith("_status")) ? v : Number(v);
  });
  const btn = form.querySelector("button[type=submit]");
  SIM.busy = true;
  btn.disabled = true;
  btn.textContent = "Firing…";
  try {
    const ans = await api(`/simulation/actions/${encodeURIComponent(id)}`, { method: "POST", body });
    SIM.log.unshift(ans);
    SIM.sel = 0;
    if (ans.world) { SIM.cat.world = ans.world; simDrawWarn(); }
    toast(`${ans.name}: ${num(ans.moved_total)} moved${ans.refused_total ? `, ${num(ans.refused_total)} refused` : ""}${ans.world && ans.world.advanced_days ? ` · ${num(ans.world.advanced_days)} day${ans.world.advanced_days === 1 ? "" : "s"} passed` : ""}`,
      ans.moved_total ? "ok" : "err");
    simDrawLog();
    simDrawEffect();
    // the rates the defaults come from move with the fleet; the forms keep what the operator typed
    api("/simulation/actions").then((cat) => { SIM.cat.rates = cat.rates; SIM.cat.world = cat.world; simDrawStats(); simDrawWarn(); }).catch(() => {});
  } catch (e) {
    toast((e && e.message) || "Could not fire", "err");
  } finally {
    SIM.busy = false;
    btn.disabled = !simCanFire();
    btn.innerHTML = `${icon("play", 13)} Fire`;
  }
}

/* ── the way back ─────────────────────────────────────────────────── */
async function simReset() {
  if (!window.confirm("Rebuild the demo dataset from scratch? Every simulated event is discarded. The rebuild runs in the background and takes a few minutes at full size.")) return;
  try {
    const st = await api("/simulation/reset", { method: "POST" });
    SIM.status.rebuild = st;
    toast("Rebuild started", "ok");
    simDrawWarn();
    simWatchRebuild();
  } catch (e) {
    toast((e && e.message) || "Could not start the rebuild", "err");
  }
}

function simWatchRebuild() {
  // The only poll of this tab, and only while a rebuild runs: the status read touches no fleet table.
  livePoll("simulation", async () => {
    const st = await api("/simulation/status");
    SIM.status = st;
    simDrawWarn();
    if (!st.rebuild.running) {
      stopLive();
      toast(st.rebuild.ok ? "Dataset rebuilt, the fleet is back to its seeded state" : "The rebuild failed: " + (st.rebuild.detail || ""), st.rebuild.ok ? "ok" : "err");
      SIM.log = [];
      RENDER.simulation();
    }
  }, 5000);
}

/* ── the panels ───────────────────────────────────────────────────── */
/* where the world stands: how many days the simulation has let pass since the seed */
const SIM_ACTION_NAME = { day: "A day of normal operation", hold: "A bottleneck" };
function simWorldLine(w) {
  if (!w) return "";
  if (!w.days_advanced) return "The dataset stands at its seed: no day has passed yet.";
  const last = w.last_action ? ` (last: ${esc(SIM_ACTION_NAME[w.last_action] || w.last_action)}, +${num(w.last_days)} day${w.last_days === 1 ? "" : "s"} at ${simTime(w.advanced_at)})` : "";
  return `<b>The dataset stands ${num(w.days_advanced)} day${w.days_advanced === 1 ? "" : "s"} later than when it was seeded</b>${last}: every date on every screen has moved by that much.`;
}

function simWarnHtml() {
  const st = SIM.status || {};
  const rb = st.rebuild || {};
  const who = st.production ? "Production refuses every write."
    : simCanFire() ? "" : "Your role can look but not fire: WAREHOUSE or ADMIN fires, ADMIN rebuilds.";
  const rebuild = rb.running
    ? `<span class="muted">Rebuilding since ${simTime(rb.started_at)}…</span>`
    : rb.finished_at ? `<span class="muted" title="${esc(rb.detail || "")}">Last rebuild ${simTime(rb.finished_at)}: ${rb.ok ? "ok" : "failed"}</span>` : "";
  const world = (SIM.cat && SIM.cat.world) || st.world || null;
  return `<span class="sim-warn__icon">${icon("alert", 18)}</span>
    <div><b>This tab writes to the demo database.</b> Every event goes through the same service calls the console uses: a move the state machine forbids is refused here too, and every move is on the device's event log. ${esc(who)} ${esc((SIM.cat || {}).calendar_note || "")}
      <div class="sim-warn__world">${simWorldLine(world)}</div></div>
    <div class="sim-warn__actions">${rebuild}${simCanReset() && !rb.running ? `<button class="btn btn--secondary btn--sm" id="sim-reset">Rebuild the dataset</button>` : ""}</div>`;
}

function simDrawWarn() {
  const host = $("#sim-warn");
  if (!host) return;
  host.innerHTML = simWarnHtml();
  const reset = $("#sim-reset");
  if (reset) reset.addEventListener("click", simReset);
}

function simStatsHtml() {
  const r = (SIM.cat && SIM.cat.rates) || {};
  const stat = (label, ic, val, hint, cls = "") =>
    `<div class="stat"><div class="stat__label">${icon(ic, 14)} ${label}</div><div class="stat__val ${cls}">${val}</div><div class="stat__hint">${hint}</div></div>`;
  const moved = SIM.log.reduce((s, a) => s + a.moved_total, 0);
  return `
    ${stat("Returns a day", "return", simFlow(r.returns_per_day), "measured from the return calendar")}
    ${stat("New devices a day", "box", simFlow(r.first_rentals_per_day), r.first_rentals_per_day == null ? esc(r.first_rentals_basis || "") : "measured: first rentals started")}
    ${stat("Second rentals a day", "layers", simFlow(r.second_rentals_per_day), "measured over the last 90 days")}
    ${stat("Sales a day", "euro", simFlow(r.sales_per_day), "measured over the last 90 days")}
    ${stat("This session", "play", num(SIM.log.length), `${num(moved)} devices moved · ${num(r.deliveries_due)} due for delivery`, "stat__val--gold")}`;
}

function simDrawStats() {
  const host = $("#sim-stats");
  if (host) host.innerHTML = simStatsHtml();
}

function simMini(label, val, hint = "") {
  return `<div class="sim-mini"><div class="sim-mini__label">${label}</div><div class="sim-mini__val">${val}</div>${hint ? `<div class="wh-note">${hint}</div>` : ""}</div>`;
}

function simHoldHtml(h) {
  const what = h.factor === 0 ? "did not drain" : `drained at ${Math.round(h.factor * 100)} % of its flow`;
  return `<div class="sim-hold">
    <div class="sim-hold__title">${esc(h.name)}: ${h.days} days passed and the station ${what} (${num(h.outflow_per_day)} a day would have left)</div>
    <div class="sim-hold__grid">
      ${simMini("On hand", `${num(h.on_hand_before)} ${icon("arrow", 11)} <b>${num(h.on_hand_after)}</b>`, `${num(h.arrived)} arrived over the ${h.days} days, staggered as they came`)}
      ${simMini("Held back", `<b>${num(h.held_back)}</b>`, `a normal ${h.days} days would have released them · ${num(h.held_back_past_target)} now past the target`)}
      ${simMini(`Crossed ${h.target_dwell_days} d while waiting`, `<b>${num(h.crossed_target)}</b>`, "units already waiting that aged past the target during the hold")}
      ${simMini(`Past ${h.target_dwell_days} d`, `${num(h.past_target_before)} ${icon("arrow", 11)} <b>${num(h.past_target_after)}</b>`, "measured before and after: the calendar moved, so the waiting stock aged")}
    </div></div>`;
}

/* the world moved: by how many days, and where the dataset now stands */
function simWorldPill(w) {
  if (!w || !w.advanced_days) return "";
  return `<span class="sim-world">${icon("clock", 12)} ${num(w.advanced_days)} day${w.advanced_days === 1 ? "" : "s"} passed · the dataset stands ${num(w.days_advanced)} day${w.days_advanced === 1 ? "" : "s"} after its seed</span>`;
}

function simDrawEffect() {
  const host = $("#sim-effect");
  if (!host) return;
  const ans = SIM.log[SIM.sel];
  if (!ans) {
    host.innerHTML = `<div class="state"><div class="state__icon">${icon("play", 22)}</div><div class="state__title">Nothing fired yet</div>
      <div class="state__sub">Fire an event above. The answer shows what moved between which compartments, what was refused and why, and which KPIs moved, before and after.</div></div>`;
    return;
  }
  const req = Object.entries(ans.requested).filter(([, v]) => v != null).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(" · ");
  const moves = ans.moved.length
    ? `<div class="sim-moves">${ans.moved.map((m) => `<span>${esc(m.from_name)} ${icon("arrow", 12)} ${esc(m.to_name)}</span><b>${num(m.units)}</b>`).join("")}</div>`
    : `<div class="muted">Nothing moved.</div>`;
  const refused = ans.refused.map((r) => `<div class="sim-refused">${plainPill(`${num(r.units)} refused`, "negative")}<span>${esc(r.what)}: ${esc(r.reason)}</span></div>`).join("");
  const notes = ans.notes.length ? `<ul class="sim-notes">${ans.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : "";
  const proceeds = ans.proceeds_eur != null ? `<div class="wh-note">Net proceeds ${euro(ans.proceeds_eur)}</div>` : "";
  const comps = ans.compartments.filter((c) => c.delta || c.over_capacity_before !== c.over_capacity_after
    || c.breach_before !== c.breach_after || c.breach_month_before !== c.breach_month_after || c.past_target_before !== c.past_target_after);
  const compRows = comps.map((c) => `<tr>
      <td><div class="cell-prod__name">${c.step}. ${esc(c.name)}</div><div class="wh-note">${esc(c.verdict_before || "")} ${icon("arrow", 10)} ${esc(c.verdict_after || "")}</div></td>
      <td class="num"><b>${num(c.on_hand_before)}</b> ${icon("arrow", 11)} <b>${num(c.on_hand_after)}</b> <span class="${c.delta > 0 ? "sim-delta--up" : c.delta < 0 ? "sim-delta--down" : "sim-delta--flat"}">${c.delta > 0 ? "+" : ""}${num(c.delta)}</span></td>
      <td class="num">${c.capacity == null ? "—" : num(c.capacity)} ${c.over_capacity_after ? plainPill(c.over_capacity_before ? "over" : "now over", "negative") : c.over_capacity_before ? plainPill("cleared", "positive") : ""}</td>
      <td class="num">${num(c.past_target_before)} ${icon("arrow", 11)} ${num(c.past_target_after)}<div class="wh-note">target ${c.target_dwell_days} d</div></td>
      <td>${simBreach(c.breach_before, c.breach_month_before)} ${icon("arrow", 11)} ${simBreach(c.breach_after, c.breach_month_after)}</td>
    </tr>`).join("");
  const kept = new Set((ans.kpis_kept || []).map((x) => x.id));
  const changed = ans.kpis.filter((k) => k.delta != null && k.delta !== 0);
  const flat = ans.kpis.filter((k) => !(k.delta != null && k.delta !== 0));
  const kpiRow = (k) => {
    const moved = k.delta != null && k.delta !== 0;
    const isKept = kept.has(k.id);
    const tone = !moved ? "sim-delta--flat" : k.better ? "sim-delta--better" : "sim-delta--worse";
    const cell = (v, why) => v == null ? `<span class="wh-na">n/a</span><div class="wh-note">${esc(why || "")}</div>` : kpiFmt(v, k.unit);
    const staleNote = k.after_stale_days > 0 ? `<div class="kpi-stale">measured ${k.after_stale_days} simulated day${k.after_stale_days === 1 ? "" : "s"} ago</div>` : "";
    return `<tr class="${moved ? "" : "sim-kpi--flat"}">
      <td><div class="kpi-name">${esc(k.name)}</div><div class="wh-note">${k.direction === "lower" ? "lower is better" : "higher is better"} · ${isKept ? `kept: not measured by this event` : `measured ${simTime(k.before_measured_at)} and ${simTime(k.after_measured_at)}`}</div></td>
      <td class="num">${cell(k.before, k.before_reason)}</td>
      <td class="num"><b>${cell(k.after, k.after_reason)}</b>${staleNote}</td>
      <td class="num ${tone}">${moved ? simSigned(k.delta, k.unit) : isKept ? "kept" : "no change"}${moved ? `<div class="wh-note">${k.better ? "better" : "worse"}</div>` : ""}</td>
    </tr>`;
  };
  const fb0 = ans.plan.first_breach_before, fb1 = ans.plan.first_breach_after;
  const breach = (fb) => fb ? `${esc(fb.name)} · ${fb.over_capacity_today ? "over capacity today" : fb.breach_now ? "breaks now" : cpMonth(fb.month)}` : "nothing breaks";
  host.innerHTML = `
    <div class="sim-effect__head">
      <div><div class="sim-effect__title">${esc(ans.name)} ${simWorldPill(ans.world)}</div><div class="wh-note">${req || "no parameters"} · fired ${simTime(ans.ran_at)} by ${esc(ans.actor)}</div></div>
      <div class="sim-effect__timing">action ${simMs(ans.timing_ms.action)}${ans.world && ans.world.advance_ms != null ? ` (calendar ${simMs(ans.world.advance_ms)})` : ""} · reads ${simMs(ans.timing_ms.reads_before + ans.timing_ms.reads_after)} · KPIs measured in ${simMs(ans.timing_ms.kpis)}</div>
    </div>
    <div class="sim-effect__grid">
      <div><div class="wh-detail__head">Moved · ${num(ans.moved_total)}</div>${moves}${proceeds}</div>
      <div><div class="wh-detail__head">Refused · ${num(ans.refused_total)}</div>${refused || `<div class="muted">Nothing refused.</div>`}</div>
    </div>
    ${ans.hold ? simHoldHtml(ans.hold) : ""}
    ${notes}
    <div class="wh-detail__head" style="margin-top:18px">Compartments that changed · ${num(comps.length)} <span class="section__hint" style="margin-left:10px;display:inline">warehouse ${num(ans.warehouse.on_hand_before)} ${icon("arrow", 10)} ${num(ans.warehouse.on_hand_after)} on hand · first breach: ${breach(fb0)} ${icon("arrow", 10)} ${breach(fb1)}</span></div>
    ${comps.length ? `<table class="tbl sim-tbl"><thead><tr><th>Compartment</th><th class="num">On hand</th><th class="num">Capacity</th><th class="num">Past target dwell</th><th>Breaks (capacity plan)</th></tr></thead><tbody>${compRows}</tbody></table>` : `<div class="muted" style="font-size:12.5px">No compartment changed.</div>`}
    <div class="wh-detail__head" style="margin-top:18px">KPIs, before and after · ${num(changed.length)} moved · ${num((ans.kpis_measured || []).length)} measured again, ${num(kept.size)} kept</div>
    <div class="wh-note" style="margin:-6px 0 8px">${esc(ans.kpis_kept_reason || "")}</div>
    <table class="tbl sim-tbl"><thead><tr><th>KPI</th><th class="num">Before</th><th class="num">After</th><th class="num">Delta</th></tr></thead><tbody>${changed.concat(flat).map(kpiRow).join("")}</tbody></table>`;
}

function simDrawLog() {
  const host = $("#sim-log");
  if (!host) return;
  if (!SIM.log.length) {
    host.innerHTML = `<div class="muted" style="font-size:12.5px;line-height:1.5">No event fired in this session. The log lives in this page; the database keeps every move on the device's event log.</div>`;
    return;
  }
  host.innerHTML = `<div class="log">${SIM.log.map((a, i) => {
    const tone = a.moved_total ? (a.refused_total ? "warning" : "positive") : "negative";
    const lines = a.moved.slice(0, 3).map((m) => `<div>${esc(m.from_name)} ${icon("arrow", 10)} ${esc(m.to_name)}: <b>${num(m.units)}</b></div>`).join("");
    const days = a.world && a.world.advanced_days ? ` · +${num(a.world.advanced_days)} day${a.world.advanced_days === 1 ? "" : "s"}` : "";
    return `<div class="log__entry sim-log__entry${i === SIM.sel ? " sim-log__entry--sel" : ""}" data-sel="${i}">
      <div class="log__rail"><div class="log__dot" style="background:${TONE[tone].dot}"></div><div class="log__line"></div></div>
      <div class="log__body"><div class="log__time">${simTime(a.ran_at)} · ${simMs(a.timing_ms.action)}${days}</div>
        <div class="attn__title">${esc(a.name)}</div>
        <div class="log__note">${num(a.moved_total)} moved${a.refused_total ? `, ${num(a.refused_total)} refused` : ""}${lines}${a.moved.length > 3 ? `<div class="muted">+${a.moved.length - 3} more</div>` : ""}</div>
      </div></div>`;
  }).join("")}</div>`;
  $$("#sim-log [data-sel]").forEach((e) => e.addEventListener("click", () => { SIM.sel = Number(e.dataset.sel); simDrawLog(); simDrawEffect(); }));
}

RENDER.simulation = async function () {
  const screen = $("#screen");
  if (!isDaas()) {
    screen.innerHTML = `${pageHead("Test run", "Simulation", "The simulation fires the day-to-day of a device fleet at the running system.")}
      <div class="panel"><div class="state"><div class="state__icon">${icon("play", 22)}</div><div class="state__title">Nothing to simulate here</div>
      <div class="state__sub">This database holds the datacenter operation; the rental cycle exists in the device-as-a-service scenario.</div></div></div>`;
    return;
  }
  try {
    const [cat, st] = await Promise.all([api("/simulation/actions"), api("/simulation/status")]);
    SIM.cat = cat;
    SIM.status = st;
    screen.innerHTML = `
      ${pageHead("Test run", "Simulation", "Fire the day-to-day of the fleet at the running system and watch the numbers move: a delivery arrives, devices go out, rentals come back, the chain clears, a partner stalls. Every event goes through the same services the console uses; every answer says what moved, what was refused and why, and which KPIs moved, before and after.")}
      <div class="sim-warn" id="sim-warn">${simWarnHtml()}</div>
      <div class="stats stats--5" id="sim-stats">${simStatsHtml()}</div>
      <div class="section">
        <div class="section__head"><span class="section__title">Events</span><span class="section__count">${cat.actions.length}</span><span class="section__hint">defaults are today's measured rates · a batch is at most ${num(cat.max_units)} devices, a hold at most ${cat.max_days} days</span></div>
        <div class="sim-grid">${cat.actions.map(simCard).join("")}</div>
      </div>
      <div class="content--rail">
        <div class="section" style="margin-bottom:0">
          <div class="section__head"><span class="section__title">What the event changed</span><span class="section__hint">compartments and KPIs before and after, the KPIs measured again the moment the event ran</span></div>
          <div class="panel sim-effect" id="sim-effect"></div>
        </div>
        <aside class="rail">
          <div class="rail__head"><span class="rail__dot"></span> This session</div>
          <div class="panel" style="padding:16px 18px" id="sim-log"></div>
        </aside>
      </div>`;
    $$("[data-sim-action]").forEach((f) => f.addEventListener("submit", (e) => { e.preventDefault(); simFire(f); }));
    const reset = $("#sim-reset");
    if (reset) reset.addEventListener("click", simReset);
    simDrawLog();
    simDrawEffect();
    if (st.rebuild && st.rebuild.running) simWatchRebuild();
  } catch (e) { screen.innerHTML = errState(e.message); }
};
