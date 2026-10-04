"""Where every KPI's goal comes from: the fleet plan, a public value, or the owner.

The plan KPIs take the capacity plan's requirement to the decimal and next to it the other
lever, the places it would take at today's speed; a public value closes from today or is
held where today is better; with neither, the goal stays empty and says who sets it; and a
target a person has set is never touched. The table of public values is checked for the
things a reader relies on: a source with a link and a date, numbers or nothing.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.models.kpi import KpiTarget
from app.services import capacity_plan, kpi_goals, kpis, warehouse


def _fleet(db_session, monkeypatch):
    """A small device fleet from the real seed: 300 rented, 100 in the warehouse."""
    from app import seed_daas
    monkeypatch.setattr(seed_daas, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(seed_daas, "N_RENTED", 300)
    monkeypatch.setattr(seed_daas, "N_WAREHOUSE", 100)
    monkeypatch.setattr(seed_daas, "N_SOLD_LAST_YEAR", 30)
    monkeypatch.setattr(seed_daas, "N_RECYCLED_LAST_YEAR", 2)
    seed_daas.seed_daas()


def test_half_year_ends_run_from_today_to_the_horizon():
    assert kpi_goals.half_year_ends(date(2026, 10, 4), date(2027, 12, 31)) == [
        date(2026, 12, 31), date(2027, 6, 30), date(2027, 12, 31)]
    assert kpi_goals.half_year_ends(date(2026, 12, 31), date(2027, 12, 31)) == [date(2027, 6, 30), date(2027, 12, 31)]
    assert kpi_goals.half_year_ends(date(2027, 12, 31), date(2027, 12, 31)) == []


def test_plan_kpis_take_the_plans_requirement(db_session, monkeypatch):
    today = date.today()
    if today >= date(2027, 12, 31):
        pytest.skip("the owner's milestones lie in the past")
    _fleet(db_session, monkeypatch)
    rows = {r["id"]: r for r in kpis.compute_all(db_session, today=today)}
    plan = capacity_plan.plan(db_session, today=today)
    W = {c["code"]: c for c in warehouse.compartments(db_session, today=today)["compartments"]}
    last = plan["milestones"][-1]
    for kid, codes in kpis.PLAN_STATIONS.items():
        r, g = rows[kid], rows[kid]["goal"]
        assert r["group"] == "plan" and r["unit"] == "days"
        # today is the sum of the stations' mean days, the dwell the plan itself uses
        assert r["current"] == round(sum(W[c]["mean_days"] for c in codes), 1), kid
        by_code = {x["code"]: x for x in last["rows"]}
        need = round(sum(by_code[c]["required_dwell_days"] for c in codes), 1)
        assert g["basis"] == "plan" and g["goal"] == need, kid
        assert g["plan"]["milestones"][-1]["need"] == need
        assert g["plan"]["milestones"][-1]["extra_places"] == sum(max(0, -by_code[c]["gap"]) for c in codes)
        assert [s["date"] for s in g["steps"]] == kpi_goals.half_year_ends(today, date.fromisoformat(str(last["date"])))
        assert g["steps"][-1]["value"] == need
        # the first milestone's need holds on its own date
        first = plan["milestones"][0]
        on_first = [s for s in g["steps"] if s["date"] == date.fromisoformat(str(first["date"]))]
        if on_first:
            assert on_first[0]["value"] == round(sum({x["code"]: x for x in first["rows"]}[c]["required_dwell_days"] for c in codes), 1)
        assert r["target_y1"] == g["steps"][0]["value"] and r["target_y3"] == need and r["placeholder"] is True
        assert g["owner"] and g["levers"], kid
    # the older KPI on the same station points to the plan KPI and invents no goal of its own
    mdm = rows["mdm_release_over_sla_pct"]["goal"]
    assert mdm["steered_by"] == "plan_mdm_hold_days" and mdm["basis"] == "owner" and mdm["goal"] is None


def test_an_industry_value_is_closed_from_today_or_held():
    today, horizon = date(2026, 10, 4), date(2027, 12, 31)
    plan = {"milestones": [{"date": horizon, "target_fleet": 1, "rows": []}], "compartments": []}
    turns = SimpleNamespace(id="stock_turns", unit="turns", direction="higher")
    g = kpi_goals.goal_for(turns, 4.61, plan, today)
    assert g["basis"] == "industry" and g["goal"] == 6.0 and g["horizon"] == horizon
    assert [s["date"] for s in g["steps"]] == kpi_goals.half_year_ends(today, horizon)
    assert g["steps"][-1]["value"] == 6.0 and 4.61 < g["steps"][0]["value"] < g["steps"][1]["value"] < 6.0
    held = kpi_goals.goal_for(turns, 7.2, plan, today)
    assert held["goal"] == 7.2 and all(s["value"] == 7.2 for s in held["steps"]) and "hold" in held["note"]
    days = SimpleNamespace(id="median_days_in_stock", unit="days", direction="lower")
    assert kpi_goals.goal_for(days, 41.0, plan, today)["goal"] == 41.0          # already below the industry value
    assert kpi_goals.goal_for(days, 60.0, plan, today)["goal"] == 45.6          # closes toward it
    weeks = SimpleNamespace(id="weeks_of_cover", unit="weeks", direction="higher")
    w = kpi_goals.goal_for(weeks, 24.9, plan, today)
    assert w["basis"] == "owner" and w["goal"] is None and w["industry"]["text"]   # a reference, too loose to steer by


def test_a_persons_target_is_never_touched(db_session):
    kpis.compute_all(db_session, today=date(2026, 9, 22))
    kpis.set_target(db_session, "stock_turns", y1=5.0, y2=5.5, y3=6.5, owner="Head of Supply Planning",
                    note="agreed in the monthly review", actor="planner@example.com")
    for _ in range(2):
        rows = {r["id"]: r for r in kpis.compute_all(db_session, today=date(2026, 9, 22), snapshot=False)}
        r = rows["stock_turns"]
        assert (r["target_y1"], r["target_y2"], r["target_y3"]) == (5.0, 5.5, 6.5)
        assert r["placeholder"] is False and r["updated_by"] == "planner@example.com"
        assert r["goal"]["basis"] == "set" and r["goal"]["goal"] == 6.5 and r["goal"]["industry"]["value"] == 6.0
    t = db_session.query(KpiTarget).filter(KpiTarget.kpi_id == "stock_turns").one()
    assert t.note == "agreed in the monthly review"


def test_export_is_the_same_record_flat(client, db_session):
    r = client.get("/api/v1/kpis/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    table = list(csv.DictReader(io.StringIO(r.text)))
    assert [row["id"] for row in table] == [k.id for k in kpis.KPIS]
    served = {x["id"]: x for x in client.get("/api/v1/kpis").json()}
    for row in table:
        g = served[row["id"]]["goal"]
        assert row["goal_basis"] == g["basis"] and row["owner"] == (g["owner"] or "")
        assert row["industry_source"] == ((g["industry"] or {}).get("source") or "")
    assert client.anon().get("/api/v1/kpis/export.csv").status_code in (401, 403)


def test_the_public_values_carry_their_source():
    ids = {k.id for k in kpis.KPIS}
    assert set(kpi_goals.BENCHMARKS) <= ids and set(kpi_goals.LEVERS) <= ids and set(kpi_goals.STEERED_BY) <= ids
    assert set(kpi_goals.OWNERS) == ids, "every KPI has exactly one owner"
    for kid, b in kpi_goals.BENCHMARKS.items():
        assert b["text"].strip() and "http" in b["source"] and "retrieved" in b["source"], kid
        assert b["value"] is None or isinstance(b["value"], (int, float)), kid
        assert b["goal"] is None or isinstance(b["goal"], (int, float)), kid
        assert b["match"].strip(), kid
    for kid, levers in kpi_goals.LEVERS.items():
        assert all(kind in ("contract", "process", "code", "missing") for _t, kind in levers), kid
    # the steered KPIs sit on the stations their plan KPI measures
    for old, new in kpi_goals.STEERED_BY.items():
        assert new in kpis.PLAN_STATIONS, old
    json.dumps(kpi_goals.BENCHMARKS)     # plain data: it serialises as the API serves it
