"""Where every KPI's goal comes from, and the goal itself, half-year by half-year.

A KPI without a goal is a number nobody can act on, and a goal without a reason is a number
nobody believes. So every goal here carries its origin, and there are exactly three:

  plan      The fleet plan sets it. The capacity plan sizes each warehouse compartment as
            throughput times dwell and says, per milestone, the dwell a compartment may not
            exceed at today's capacity. The four plan KPIs measure that dwell, so their goal
            is the plan's requirement, read from the plan, never typed. Next to it stands
            the other lever: the extra places it would take at today's speed.
  industry  A public value with its source: a benchmark, a peer's published figure, a
            norm. Where today's value is already better, the goal is to hold it; otherwise
            a straight line from today to that value at the plan's horizon.
  owner     Neither the plan nor a comparable public value: the goal is left to the role
            that owns the KPI. Never a made-up number in its place.

**Precedence: the plan binds where it sets a number; the industry is the reference.** And
a target a person has set through the API (``KpiTarget.placeholder`` False) wins over all
three: this module writes only the rows nobody has touched, the ones the seed or this
model wrote. Its own writes carry ``updated_by = GOAL_MODEL``. A stored state is never the
authority over a person's change.

**Half-years.** The checkpoints are the half-year ends from today to the horizon, the last
plan milestone. A plan goal is the requirement at each milestone, interpolated in between.

The benchmarks were researched on 04.10.2026. Every value names its publisher, date and
URL; ``match`` says how well it fits this KPI's definition, because a proxy must not pass
for a benchmark.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.kpi import KpiTarget

GOAL_MODEL = "goal-model"
FALLBACK_HORIZON = date(2027, 12, 31)    # used only when the capacity plan has no future milestone

# One owner per KPI: the role that answers for the number. A proposal until the house names people.
OWNERS: dict[str, str] = {
    "plan_new_stock_days": "Head of Operations", "plan_return_to_ready_days": "Head of Operations",
    "plan_mdm_hold_days": "Head of Customer Success", "plan_second_life_days": "Head of Sales",
    "second_rental_share_pct": "Head of Sales", "returns_overdue": "Head of Customer Success",
    "early_return_share_pct": "Head of Customer Success", "mdm_release_over_sla_pct": "Head of Customer Success",
    "return_to_ready_days": "Head of Operations", "sellable_reach_months": "Head of Recommerce",
    "swap_buffer_months": "Head of Operations", "second_life_reach_months": "Head of Operations",
    "resale_share_of_purchase_pct": "Head of Recommerce", "recycling_share_pct": "Head of Recommerce",
    "capacity_committed_pct": "Head of Operations", "weeks_of_cover": "Head of Supply Planning",
    "items_at_risk": "Head of Supply Planning", "safety_stock_coverage_pct": "Head of Supply Planning",
    "stock_value_eur": "CFO", "carrying_cost_eur_per_day": "CFO", "aging_stock_pct": "Head of Supply Planning",
    "median_days_in_stock": "Head of Operations", "dead_stock_value_eur": "CFO", "stock_turns": "Head of Supply Planning",
    "dock_to_deploy_days": "Head of Operations", "inbound_overdue_pct": "Head of Procurement",
    "on_time_delivery_pct": "Head of Procurement", "negotiation_gap_eur": "Head of Procurement",
    "products_above_target_pct": "Head of Procurement", "spend_under_contract_pct": "Head of Procurement",
    "top3_supplier_share_pct": "Head of Procurement", "auto_placed_pct": "Head of Procurement",
    "requisition_cycle_hours": "Head of Procurement", "forecast_mape_pct": "Head of Supply Planning",
    "contracts_needing_action": "Head of Procurement", "single_sourced_products_pct": "Head of Procurement",
}

# What has to change in the business to move the number, and where the lever sits:
# contract (a clause), process (an agreement, a shift, a rhythm), code (a setting in the
# system), missing (does not exist yet). Most levers are not software.
LEVERS: dict[str, list[tuple[str, str]]] = {
    "plan_mdm_hold_days": [("Release deadline as a clause in the customer contract", "contract"),
                           ("Release check at the return booking, before the parcel ships", "process")],
    "plan_return_to_ready_days": [("Places and shifts in wipe and grading", "missing"),
                                  ("Fixed price and turnaround commitment from the refurbisher", "contract"),
                                  ("Handover to the refurbisher in a fixed rhythm and batch size", "process")],
    "plan_second_life_days": [("Offer refurbished devices in every renewal and new deal", "process"),
                              ("Second-life price list per grade", "missing")],
    "plan_new_stock_days": [("Delivery dates committed per source, ordered to the placement plan", "contract"),
                            ("Enrolment and staging capacity sized to the placements", "process")],
    "capacity_committed_pct": [("Capacity per compartment set by a person, not derived from stock", "missing"),
                               ("Speed up the slowest stations before adding space", "process")],
    "swap_buffer_months": [("Buffer sized to the fleet and reviewed monthly", "process")],
    "returns_overdue": [("Renewal outreach 90 days before the term ends", "process")],
    "early_return_share_pct": [("Pre-return self-test in the customer portal", "missing")],
    "sellable_reach_months": [("Price floor per grade before any channel push", "missing")],
    "resale_share_of_purchase_pct": [("Freeze the expected resale value on the return day", "missing")],
    "weeks_of_cover": [("A ceiling on cover, not only a floor", "code")],
    "aging_stock_pct": [("Price floor per grade before any channel push", "missing")],
    "spend_under_contract_pct": [("Call-off obligation: orders go through the framework", "process")],
    "on_time_delivery_pct": [("Lead-time commitment per source", "contract")],
    "inbound_overdue_pct": [("Lead-time commitment per source", "contract")],
    "single_sourced_products_pct": [("Second distributor per model family", "process"), ("Preferred rank per source", "code")],
    "top3_supplier_share_pct": [("Second distributor per model family", "process")],
    "auto_placed_pct": [("Rules decide, people handle the exceptions", "missing")],
    "requisition_cycle_hours": [("Rules decide, people handle the exceptions", "missing")],
    "forecast_mape_pct": [("Plan on the fleet path and the return calendar, not on history alone", "process")],
    "contracts_needing_action": [("Renewal start 90 days before the notice date", "process")],
}

# An older KPI on a station the plan sizes: its goal comes from the plan KPI next to it.
STEERED_BY: dict[str, str] = {
    "mdm_release_over_sla_pct": "plan_mdm_hold_days",
    "return_to_ready_days": "plan_return_to_ready_days",
    "second_life_reach_months": "plan_second_life_days",
    "dock_to_deploy_days": "plan_new_stock_days",
}

# Public values, researched 04.10.2026. ``value`` is in the KPI's own unit (None where only a
# text reference exists); ``goal`` is the value a goal may be set to, None where the match is
# too loose to steer by.
BENCHMARKS: dict[str, dict] = {
    'aging_stock_pct': {
        "value": None, "goal": None, "match": 'proxy',
        "text": 'No public share of stock older than 90 days. Why it matters: US notebook and desktop prices fell 12 % over a typical four-month product cycle (Federal Reserve Bank of San Francisco, NPD data 2001 to 2009).',
        "source": 'Copeland and Shapiro, Price Setting in an Innovative Market, FRBSF Working Paper 2013-04, https://www.frbsf.org/wp-content/uploads/wp2013-04.pdf, retrieved 04.10.2026',
    },
    'auto_placed_pct': {
        "value": 73, "goal": 73, "match": 'top solutions',
        "text": 'The Hackett Group: companies on the top purchase-to-pay solutions reach 73 % touchless requisition-to-order automation. A value for the best tools, not a cross-company median.',
        "source": 'The Hackett Group, P2P Value Matrix, 23.08.2023, https://www.thehackettgroup.com/digital-world-class-matrix-quantifies-the-value-realized-from-purchase-to-pay-software-solutions-providers/, retrieved 04.10.2026',
    },
    'capacity_committed_pct': {
        "value": 85, "goal": 85, "match": 'direct, older data',
        "text": 'WERC DC Measures: median average warehouse capacity used 85 %, best-in-class from 92 % (2018 data, quoted by Honeywell/CSSI); a WERC study team put the ideal closer to 80 % to keep room to react. Ours also counts inbound already ordered.',
        "source": 'CSSI / Honeywell, warehouse DC infographic citing WERC 2018 DC Measures, https://cssi.com/wp-content/uploads/CSSI_warehouse-dc-infographic.pdf, retrieved 04.10.2026',
    },
    'carrying_cost_eur_per_day': {
        "value": None, "goal": None, "match": 'rate only',
        "text": "No benchmark for euros a day. The rate behind it: APQC median inventory carrying cost 10.0 % of average inventory value a year (n = 6,468); textbooks give 18 to 25 % and wider. The tool's rate is capital only, a placeholder of the CFO.",
        "source": 'APQC, inventory carrying cost percentage, https://www.apqc.org/what-we-do/benchmarking/open-standards-benchmarking/measures/inventory-carrying-cost-percentage, retrieved 04.10.2026',
    },
    'contracts_needing_action': {
        "value": None, "goal": None, "match": 'process norm',
        "text": 'A count has no benchmark. The norm behind it: start renewals at least 90 days ahead, because renewals take 82 days from request to signature against 40 for a new contract (software data, Vertice Q1 2026).',
        "source": 'Vertice, Procurement cycle time, July 2026, https://www.vertice.one/insights/procurement-cycle-time, retrieved 04.10.2026',
    },
    'dead_stock_value_eur': {
        "value": None, "goal": None, "match": 'proxy',
        "text": 'APQC median inventory obsolescence 1.6 % of total inventory (n = 4,612), with the no-movement period set per industry, not at our 180 days. Today we carry none.',
        "source": 'APQC, inventory obsolescence percentage, https://www.apqc.org/what-we-do/benchmarking/open-standards-benchmarking/measures/inventory-obsolescence-percentage, retrieved 04.10.2026',
    },
    'dock_to_deploy_days': {
        "value": None, "goal": None, "match": 'partial',
        "text": 'Dock to stock (arrival until put away and booked) runs 15 hours at the APQC median (n = 3,560) and under 3.1 hours at WERC best-in-class (2026). Our days include waiting for a customer, which is what the plan limits.',
        "source": 'APQC, dock-to-stock cycle time in hours for supplier deliveries, https://www.apqc.org/resources/benchmarking/open-standards-benchmarking/measures/dock-stock-cycle-time-hours-supplier, retrieved 04.10.2026',
    },
    'early_return_share_pct': {
        "value": None, "goal": None, "match": 'proxy',
        "text": 'No comparable public rate for early or defect contract ends. For scale: consumer smartphones in frontline use fail 12.8 % a year (VDC Research 2021, via a Zebra document); manufacturer returns of smartphones in the first six months run 4 to 5 % (TL 9000, 2010 to 2011 data).',
        "source": 'QuEST Forum TL 9000, Simple and Complex Wireless Devices, https://tl9000.org/resources/documents/12_SimpleComplex_WirelessDevices_Final.pdf, retrieved 04.10.2026',
    },
    'forecast_mape_pct': {
        "value": 33, "goal": 33, "match": 'proxy',
        "text": "IBF retail surveys: 33 % MAPE per item one quarter ahead, 25 % per category; APQC product-family monthly MAPE is 6 % at the median, a much coarser level. Cross-company forecast benchmarks are weak (Kolassa 2008); below them, the goal is to hold today's level.",
        "source": 'Institute of Business Forecasting, What are the benchmarks in retail forecasting accuracy, 22.06.2018, https://demand-planning.com/2018/06/22/what-are-the-benchmarks-in-retail-forecasting-accuracy/, retrieved 04.10.2026',
    },
    'inbound_overdue_pct': {
        "value": None, "goal": None, "match": 'inverse proxy',
        "text": 'No public share of open order lines past the confirmed date. Its mirror image: APQC median supplier on-time delivery 90.0 %, and only 81.0 % of orders arrive by the originally requested date.',
        "source": 'APQC, percentage of supplier on-time delivery, https://www.apqc.org/what-we-do/benchmarking/open-standards-benchmarking/measures/percentage-supplier-time-delivery, retrieved 04.10.2026',
    },
    'items_at_risk': {
        "value": None, "goal": None, "match": 'proxy',
        "text": 'No public count of products at stock-out risk. As an outcome: APQC median 1.3 % of order lines unfilled for stock-outs or capacity (n = 348). Today none of our products is at risk.',
        "source": 'APQC, percentage of total sales order lines not fulfilled, https://apqc.org/what-we-do/benchmarking/open-standards-benchmarking/measures/percentage-total-sales-order-line, retrieved 04.10.2026',
    },
    'median_days_in_stock': {
        "value": 45.6, "goal": 45.6, "match": 'proxy',
        "text": "APQC median inventory days of supply 45.6 days (n = 6,951); listed IT distributors run 53 days (TD SYNNEX, quarter to 30.11.2025) and 61 days (Arrow, Q2 2026), averages rather than medians. Below them, the goal is to hold today's level.",
        "source": 'APQC, inventory days of supply, https://www.apqc.org/what-we-do/benchmarking/open-standards-benchmarking/measures/inventory-days-supply, retrieved 04.10.2026',
    },
    'negotiation_gap_eur': {
        "value": None, "goal": None, "match": 'rate only',
        "text": "No benchmark in euros. As a rate: procurement delivers 2.0 % of spend in cost takeout at the APQC median (n = 2,431); Ardent's CPOs report a 7.6 % savings rate for 2025, best-in-class 9.2 %.",
        "source": 'APQC, typical savings achieved through cost takeout, https://www.apqc.org/resources/benchmarking/open-standards-benchmarking/measures/typical-savings-achieved-through-cost, retrieved 04.10.2026',
    },
    'on_time_delivery_pct': {
        "value": 90.0, "goal": 90.0, "match": 'direct',
        "text": 'APQC median supplier on-time delivery 90.0 % (n = 4,648). Only the median is public; the top quartile is for members.',
        "source": 'APQC, percentage of supplier on-time delivery, https://www.apqc.org/what-we-do/benchmarking/open-standards-benchmarking/measures/percentage-supplier-time-delivery, retrieved 04.10.2026',
    },
    'products_above_target_pct': {
        "value": None, "goal": None, "match": 'none',
        "text": 'No public share of products priced above should-cost. A sourcing rule of thumb calls a gap of about 20 % substantial and under 10 % probably fair.',
        "source": 'Purolator International, Should-cost modeling helps control costs, 02.10.2012, https://www.purolatorinternational.com/should-cost-modeling-helps-control-costs/, retrieved 04.10.2026',
    },
    'recycling_share_pct': {
        "value": 4, "goal": 4, "match": 'direct',
        "text": 'CHG-MERIDIAN, a German IT leasing company: 4 % of its IT lease returns went to recycling in 2025, 96 % were remarketed.',
        "source": 'CHG-MERIDIAN, Circular economy, https://www.chg-meridian.com/global-en/sustainability/circular-economy/, retrieved 04.10.2026',
    },
    'requisition_cycle_hours': {
        "value": 48, "goal": 48, "match": 'direct, converted',
        "text": 'APQC median cycle time from requisition to released purchase order for goods: 2.0 days including weekends (n = 1,181), shown here in hours. Digital world-class teams are 58 % faster (Hackett).',
        "source": 'APQC, cycle time to issue a purchase order for goods, https://www.apqc.org/what-we-do/benchmarking/open-standards-benchmarking/measures/cycle-time-issue-purchase-order-goods, retrieved 04.10.2026',
    },
    'resale_share_of_purchase_pct': {
        "value": 37, "goal": 37, "match": 'proxy',
        "text": "A B2B leasing residual of 37 % of the purchase price after two years and 24 % after three (Tech Data Switzerland, guaranteed value, MacBook Pro 16, 2020). The closest public definition; above it, the goal is to hold today's level.",
        "source": 'it-markt.ch, IT-Leasing, 26.02.2020, https://www.it-markt.ch/news/2020-02-26/it-leasing-eine-win-win-situation-fuer-reseller-und-ihre-geschaeftskunden, retrieved 04.10.2026',
    },
    'return_to_ready_days': {
        "value": 5, "goal": None, "match": 'vendor claim',
        "text": 'Swappie for Business states about 5 days turnaround (vendor, buyback, start and end not defined); Reconext recommends 3 to 7 days from decommission to processing (vendor blog). No independent benchmark.',
        "source": 'Swappie for Business, ITAD, https://business.swappie.com/services/itad, retrieved 04.10.2026',
    },
    'safety_stock_coverage_pct': {
        "value": 95, "goal": 95, "match": 'target norm, vendor',
        "text": "Target service levels are typically above 95 % (A items 96 to 98 %); a planning norm from a forecasting vendor, not a measured benchmark. Above it, the goal is to hold today's level.",
        "source": 'Lokad, Service level definition, revised March 2014, https://www.lokad.com/service-level-definition/, retrieved 04.10.2026',
    },
    'second_rental_share_pct': {
        "value": 42, "goal": 42, "match": 'proxy',
        "text": "Grover: 42 % of its rentals in 2024 were devices on at least their second rental. A consumer rental business, and a share of rentals over the year, not of the fleet on one day; the plan's fresh devices dilute our share.",
        "source": 'Grover, Impact Report 2024, retrieved 04.10.2026, https://www.grover.com/de-en/g-about/impact-report',
    },
    'single_sourced_products_pct': {
        "value": None, "goal": None, "match": 'none',
        "text": 'No public share of single-sourced products. 73 % of supply chain leaders report progress on dual sourcing (McKinsey, n = 88, 2024). For finished devices one maker per model is structural; the share to manage is distributors per model.',
        "source": 'McKinsey, Supply chain risk survey 2024, 14.10.2024, https://karriere.mckinsey.de/capabilities/operations/our-insights/supply-chain-risk-survey-2024, retrieved 04.10.2026',
    },
    'spend_under_contract_pct': {
        "value": 78.2, "goal": 92.8, "match": 'direct',
        "text": 'Ardent Partners, Procurement Metrics That Matter in 2026 (311 CPOs): contract-compliant spend 78.2 % on average, 92.8 % at best-in-class; spend under management 69.3 % and 90.2 %. We are above the average, so the goal is best-in-class.',
        "source": 'GEP (sponsor) on Ardent Partners, Procurement Metrics That Matter in 2026, 31.07.2026, https://www.gep.com/blog/mind/how-cpos-measure-procurement-success, retrieved 04.10.2026',
    },
    'stock_turns': {
        "value": 6.0, "goal": 6.0, "match": 'peer',
        "text": 'Arrow Electronics turned its inventory 6.0 times a year in Q2 2026 (group level, primary source); the cross-industry APQC median is 8.0 turns (n = 5,349).',
        "source": 'Arrow Electronics, Q2 2026 earnings presentation, slide 9, 06.08.2026, https://s28.q4cdn.com/374293242/files/doc_financials/2026/q2/2Q26-Earnings-Presentation_FINAL.pdf, retrieved 04.10.2026',
    },
    'swap_buffer_months': {
        "value": None, "goal": None, "match": 'proxy',
        "text": "Spare pools are sized as a share of the active fleet, most often 5 to 10 % (an integrator's rule of thumb, rugged devices). Ours is counted in months of swaps instead; the plan needs the stock to grow with the fleet.",
        "source": 'CSSI Technologies, Tips for Maintaining Your Mobile Device Spare Pool, https://cssi.com/?p=3843, retrieved 04.10.2026',
    },
    'top3_supplier_share_pct': {
        "value": None, "goal": None, "match": 'structural',
        "text": 'Across all spend, the top ten suppliers take 35 % at the APQC median, but a device buyer is concentrated by the market itself: Samsung and Apple ship 42.7 % of smartphones, Lenovo, HP and Dell 57.1 % of PCs (IDC, Q2 2026). The manageable share is distributors per model.',
        "source": 'IDC, Q2 2026 PC and smartphone press releases, 08.07.2026 and 13.07.2026, https://www.idc.com/resource-center/press-releases/2q26-pc-top5/, retrieved 04.10.2026',
    },
    'weeks_of_cover': {
        "value": None, "goal": None, "match": 'proxy',
        "text": 'APQC median finished-goods days of supply 36.5 days (n = 12,091); US computer wholesalers hold 0.80 months of sales (Census, July 2026). Our cover is several times that: the tool calls more cover better, but this KPI needs a ceiling, not a floor.',
        "source": 'APQC Open Standards Benchmarking, finished goods inventory days of supply, https://www.apqc.org/resources/benchmarking/open-standards-benchmarking/measures/finished-goods-inventory-days-supply, retrieved 04.10.2026',
    },
}


# ---------------------------------------------------------------------------------------------


def half_year_ends(today: date, horizon: date) -> list[date]:
    """30.06. and 31.12. after today, up to and including the horizon."""
    out, y = [], today.year
    while True:
        for m, d in ((6, 30), (12, 31)):
            x = date(y, m, d)
            if today < x <= horizon:
                out.append(x)
        if date(y, 12, 31) >= horizon:
            return out
        y += 1


def _interp(points: list[tuple[date, float]], when: date) -> float:
    """Linear between (date, value) points; flat before the first and after the last."""
    if when <= points[0][0]:
        return points[0][1]
    for (d0, v0), (d1, v1) in zip(points, points[1:]):
        if when <= d1:
            span = (d1 - d0).days
            return v0 + (v1 - v0) * ((when - d0).days / span if span else 1.0)
    return points[-1][1]


def plan_needs(plan: Optional[dict], codes: tuple[str, ...]) -> Optional[dict]:
    """What the capacity plan asks of a station path at each milestone: the dwell it may not
    exceed at today's capacity, and the places it would need at today's dwell instead."""
    if not plan or not plan.get("milestones"):
        return None
    comp = {c["code"]: c for c in plan.get("compartments", [])}
    out = []
    for ms in plan["milestones"]:
        rows = {r["code"]: r for r in ms["rows"]}
        if any(c not in rows for c in codes):
            return None
        dwell = [rows[c]["required_dwell_days"] for c in codes]
        out.append({
            "date": ms["date"], "fleet": ms["target_fleet"],
            "need": (round(sum(dwell), 1) if all(v is not None for v in dwell) else None),
            "extra_places": int(sum(max(0, -(rows[c]["gap"] or 0)) for c in codes)),
            "fits_today": all(bool(rows[c]["fits"]) for c in codes),
        })
    return {
        "milestones": out,
        "capacity_placeholder": any(comp.get(c, {}).get("capacity_placeholder", True) for c in codes),
        "capacity_owner": next((comp[c].get("capacity_owner") for c in codes if c in comp), None),
        "basis": "required dwell = capacity / required throughput, per station, at each milestone of the fleet plan",
    }


def horizon_of(plan: Optional[dict]) -> date:
    ms = (plan or {}).get("milestones") or []
    return date.fromisoformat(str(ms[-1]["date"])[:10]) if ms else FALLBACK_HORIZON


def goal_for(kpi, current: Optional[float], plan: Optional[dict], today: date, plan_codes: Optional[tuple] = None) -> dict:
    """The goal record of one KPI: origin, goal, half-year steps, the plan's need, the public value."""
    horizon = horizon_of(plan)
    checkpoints = half_year_ends(today, horizon)
    bench = BENCHMARKS.get(kpi.id)
    need = plan_needs(plan, plan_codes) if plan_codes else None
    rec = {
        "basis": "owner", "goal": None, "unit": kpi.unit, "horizon": horizon,
        "steps": [], "plan": need, "industry": bench, "owner": OWNERS.get(kpi.id),
        "levers": [{"text": t, "kind": k} for t, k in LEVERS.get(kpi.id, [])],
        "steered_by": STEERED_BY.get(kpi.id), "note": None,
    }
    if need and need["milestones"] and all(m["need"] is not None for m in need["milestones"]):
        pts = [(date.fromisoformat(str(m["date"])[:10]), m["need"]) for m in need["milestones"]]
        rec.update(basis="plan", goal=pts[-1][1],
                   steps=[{"date": d, "value": round(_interp(pts, d), 1)} for d in checkpoints],
                   note="the fleet plan: the dwell this path may not exceed at today's capacity")
    elif bench and bench.get("goal") is not None:
        g = float(bench["goal"])
        if current is None:
            rec.update(basis="industry", goal=g, note="industry value; the steps follow the first measurement")
        else:
            better = current <= g if kpi.direction == "lower" else current >= g
            goal = current if better else g
            span = max(1, (horizon - today).days)
            rec.update(basis="industry", goal=round(goal, 2),
                       steps=[{"date": d, "value": round(current + (goal - current) * min(1.0, (d - today).days / span), 2)}
                              for d in checkpoints],
                       note=("today is already better than the industry value: hold it" if better
                             else "a straight line from today to the industry value at the plan's horizon"))
    else:
        rec["note"] = ("the plan sets this flow in days: see " + STEERED_BY[kpi.id]) if kpi.id in STEERED_BY \
            else "no plan need and no comparable public value: the owner sets the goal"
    return rec


def _targets_from(rec: dict) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """The goal in the yearly target fields the status reads: the first checkpoint, the horizon, the horizon."""
    if rec["goal"] is None:
        return None, None, None
    if not rec["steps"]:
        return rec["goal"], rec["goal"], rec["goal"]
    return rec["steps"][0]["value"], rec["goal"], rec["goal"]


def sync_target(db: Session, kpi, rec: dict) -> KpiTarget:
    """Write the goal into the KPI's target row, unless a person owns that row.

    A row is the model's to write when nobody set it: ``placeholder`` still True (the seed's
    10/20/30 % rule or this model's last write). A person's row is left exactly as it is, and
    the record then says so instead of the model's goal.
    """
    t = db.execute(select(KpiTarget).where(KpiTarget.kpi_id == kpi.id)).scalar_one_or_none()
    if t is not None and not t.placeholder:
        rec.update(basis="set", goal=(t.target_y3 if t.target_y3 is not None else t.target_y2 if t.target_y2 is not None
                                      else t.target_y1),
                   owner=t.owner or rec["owner"], note=f"set by {t.updated_by or 'a person'}: the plan and the industry stand as reference",
                   steps=[])
        return t
    y1, y2, y3 = _targets_from(rec)
    note = f"{rec['basis']}: {rec['note']}"
    if t is None:
        t = KpiTarget(kpi_id=kpi.id)
        db.add(t)
    if (t.target_y1, t.target_y2, t.target_y3, t.owner, t.note, t.updated_by) != (y1, y2, y3, rec["owner"], note, GOAL_MODEL):
        t.target_y1, t.target_y2, t.target_y3 = y1, y2, y3
        t.owner, t.note, t.placeholder, t.updated_by = rec["owner"], note, True, GOAL_MODEL
        db.flush()
    return t
