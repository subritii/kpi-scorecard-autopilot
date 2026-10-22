"""Tests for app.drivers: hand-worked ranking first, then the planted events."""

import ast
import csv
import inspect
import json
from collections import defaultdict
from decimal import Decimal as D

import pytest

from app import drivers
from app.config import REPO_ROOT
from app.drivers import WeekData

ALL_SOURCES = frozenset({"qbo_profit_and_loss", "qbo_balance_sheet", "qbo_ar_aging", "sales_sheet"})


def amounts(**kv) -> dict:
    return {k: D(v) for k, v in kv.items()}


def week(*rows, loaded=ALL_SOURCES) -> WeekData:
    """week(("cogs", {"account": "Labor"}, "500"), ...) with string values."""
    return WeekData(tuple((m, detail, D(v)) for m, detail, v in rows), frozenset(loaded))


# --- rank ----------------------------------------------------------------------


def test_rank_hand_worked():
    # Changes: A +50, B 0, C -30. Total movement |50| + |0| + |-30| = 80.
    ranked = drivers.rank(
        current=amounts(A="150", B="100", C="50"), prior=amounts(A="100", B="100", C="80")
    )
    assert [d.name for d in ranked] == ["A", "C", "B"]
    a, c, b = ranked
    assert (a.prior, a.current, a.change) == (D("100"), D("150"), D("50"))
    assert c.change == D("-30")  # sign kept: C moved against the overall increase
    assert (a.share_of_movement, c.share_of_movement, b.share_of_movement) == (
        D("0.625"),
        D("0.375"),
        D("0"),
    )


def test_rank_orders_by_size_not_sign():
    ranked = drivers.rank(current=amounts(up="110", down="40"), prior=amounts(up="100", down="100"))
    assert [d.name for d in ranked] == ["down", "up"]  # -60 outranks +10


def test_rank_ties_break_by_name():
    # +10 and -10 are the same size; alphabetical order decides, whatever the input order.
    current = amounts(beta="110", alpha="90")
    prior = amounts(beta="100", alpha="100")
    assert [d.name for d in drivers.rank(current, prior)] == ["alpha", "beta"]
    reversed_current = dict(reversed(list(current.items())))
    reversed_prior = dict(reversed(list(prior.items())))
    assert [d.name for d in drivers.rank(reversed_current, reversed_prior)] == ["alpha", "beta"]


def test_rank_includes_names_from_either_week():
    # A new customer counts its full amount as an increase; a lost one, a decrease.
    ranked = drivers.rank(current=amounts(new="40"), prior=amounts(gone="25"))
    by_name = {d.name: d for d in ranked}
    assert (by_name["new"].prior, by_name["new"].change) == (D("0"), D("40"))
    assert (by_name["gone"].current, by_name["gone"].change) == (D("0"), D("-25"))


def test_rank_without_prior_week():
    ranked = drivers.rank(current=amounts(A="30", B="10"), prior={})
    assert [(d.name, d.change) for d in ranked] == [("A", D("30")), ("B", D("10"))]


def test_rank_no_movement_has_no_share():
    ranked = drivers.rank(current=amounts(A="5", B="7"), prior=amounts(A="5", B="7"))
    assert [d.name for d in ranked] == ["A", "B"]
    assert all(d.change == 0 and d.share_of_movement is None for d in ranked)


def test_rank_limit():
    ranked = drivers.rank(amounts(A="9", B="5", C="1"), {}, limit=2)
    assert [d.name for d in ranked] == ["A", "B"]


def test_rank_rejects_float():
    with pytest.raises(TypeError):
        drivers.rank(current={"A": 1.5}, prior={})


# --- contributions and drivers_for --------------------------------------------


def test_contributions_sums_metrics_per_dimension():
    rows = [
        ("ar_61_90", {"customer": "X"}, D("100")),
        ("ar_90_plus", {"customer": "X"}, D("50")),
        ("ar_61_90", {"customer": "Y"}, D("20")),
        ("revenue", {"customer": "X"}, D("999")),  # other metric: ignored
    ]
    got, missing = drivers.contributions(rows, frozenset({"ar_61_90", "ar_90_plus"}), "customer")
    assert got == {"X": D("150"), "Y": D("20")}
    assert missing == 0


def test_drivers_for_ar_combines_both_buckets_and_reports_net_change():
    current = week(
        ("ar_61_90", {"customer": "X"}, "100"),
        ("ar_90_plus", {"customer": "X"}, "50"),
        ("ar_61_90", {"customer": "Y"}, "10"),
    )
    prior = week(("ar_61_90", {"customer": "X"}, "40"), ("ar_61_90", {"customer": "Y"}, "30"))
    result = drivers.drivers_for("ar_over_60", current, prior)
    x, y = result.drivers
    assert (x.name, x.prior, x.current, x.change) == ("X", D("40"), D("150"), D("110"))
    assert y.change == D("-20")
    # Net change of the KPI: 160 - 70 = +90 (while total movement is 130).
    assert result.net_change == D("90")
    assert result.contributors_net_change == D("90")
    assert result.denominator is None  # not a ratio KPI
    assert result.warnings == []


def test_net_change_is_none_when_a_week_is_no_data():
    current = week(("ar_61_90", {"customer": "X"}, "100"))
    prior = week(loaded=set())  # AR report never arrived last week
    assert drivers.drivers_for("ar_over_60", current, prior).net_change is None


def test_missing_dimension_grouped_as_unassigned_with_warning():
    current = week(
        ("cogs", {"account": "Labor"}, "500"),
        ("cogs", {}, "300"),  # no account on this row
        ("revenue", {"customer": "A"}, "1000"),
    )
    prior = week(("cogs", {"account": "Labor"}, "500"), ("revenue", {"customer": "A"}, "1000"))
    result = drivers.drivers_for("gross_margin_pct", current, prior)
    # Ranked like any other contributor: it is the biggest mover here.
    assert result.drivers[0].name == drivers.UNASSIGNED
    assert result.drivers[0].change == D("300")
    assert result.warnings == ["1 current-week row(s) had no 'account'; grouped as Unassigned"]


def test_drivers_for_unknown_formula_raises():
    with pytest.raises(KeyError):
        drivers.drivers_for("ebitda", week(), week())


# --- ratio KPIs: the denominator ------------------------------------------------


def test_revenue_drop_with_flat_costs_flags_the_denominator():
    # Margin falls 50% -> 37.4% because revenue dropped; costs barely moved.
    # The top cost contributor (+1 software) must not take the blame.
    prior = week(
        ("revenue", {"customer": "A"}, "1000"),
        ("cogs", {"account": "Labor"}, "500"),
        ("cogs", {"account": "Software"}, "10"),
    )
    current = week(
        ("revenue", {"customer": "A"}, "800"),
        ("cogs", {"account": "Labor"}, "500"),
        ("cogs", {"account": "Software"}, "11"),
    )
    result = drivers.drivers_for("gross_margin_pct", current, prior)
    assert result.drivers[0].name == "Software"
    den = result.denominator
    assert (den.metric, den.prior, den.current, den.change) == (
        "revenue",
        D("1000"),
        D("800"),
        D("-200"),
    )
    # Alone, revenue 1000 -> 800 moves margin 49.0 -> 36.25 = -12.75 points;
    # Software +1 alone moves it 49.0 -> 48.9 = -0.1 points.
    assert den.effect_on_kpi == D("-12.75")
    assert den.top_contributor_effect_on_kpi == D("-0.1")
    assert den.exceeds_top_contributor is True


def test_cost_increase_with_flat_revenue_does_not_flag():
    prior = week(("revenue", {"customer": "A"}, "1000"), ("cogs", {"account": "Subs"}, "100"))
    current = week(("revenue", {"customer": "A"}, "1000"), ("cogs", {"account": "Subs"}, "250"))
    den = drivers.drivers_for("gross_margin_pct", current, prior).denominator
    assert den.change == D("0")
    assert den.exceeds_top_contributor is False


def test_inventory_days_denominator_is_cogs():
    # COGS drops 10,000 -> 5,000 with inventory flat: days double from 49 to 98.
    prior = week(
        ("inventory_balance", {"category": "Pipe"}, "70000"),
        ("cogs", {"account": "Product"}, "10000"),
    )
    current = week(
        ("inventory_balance", {"category": "Pipe"}, "70000"),
        ("cogs", {"account": "Product"}, "5000"),
    )
    result = drivers.drivers_for("inventory_days", current, prior)
    assert result.net_change == D("49")
    assert result.denominator.metric == "cogs"
    assert result.denominator.effect_on_kpi == D("49")
    assert result.denominator.exceeds_top_contributor is True


# --- planted events --------------------------------------------------------------


def load_weeks() -> dict:
    """(client_id, week_start) -> WeekData from the seed CSVs."""
    seed = REPO_ROOT / "data" / "seed"
    rows, loaded = defaultdict(list), defaultdict(set)
    with open(seed / "source_facts.csv", newline="") as f:
        for r in csv.DictReader(f):
            rows[(r["client_id"], r["week_start"])].append(
                (r["metric_key"], json.loads(r["detail_json"]), D(r["value"]))
            )
    with open(seed / "source_loads.csv", newline="") as f:
        for r in csv.DictReader(f):
            loaded[(r["client_id"], r["week_start"])].add(r["source"])
    return {k: WeekData(tuple(rows[k]), frozenset(loaded[k])) for k in loaded}


def planted_event_weeks():
    gt = json.loads((REPO_ROOT / "data" / "seed" / "ground_truth.json").read_text())
    weeks = gt["week_starts"]
    for e in gt["events"]:
        for w in e["weeks"]:
            prior = weeks[weeks.index(w["week_start"]) - 1]
            yield pytest.param(e, w["week_start"], prior, id=f"{e['formula_key']}-wk{w['week']}")


@pytest.mark.parametrize("event, week_start, prior_week", list(planted_event_weeks()))
def test_planted_driver_ranks_first(event, week_start, prior_week):
    weeks = load_weeks()
    result = drivers.drivers_for(
        event["formula_key"],
        weeks[(event["client_id"], week_start)],
        weeks[(event["client_id"], prior_week)],
    )
    expected = event["top_drivers"]
    # The first-ranked driver is a planted cause, and the top N are exactly the N planted causes.
    assert result.drivers[0].name in expected
    assert sorted(d.name for d in result.drivers[: len(expected)]) == sorted(expected)
    # For the planted ratio events the contributor side really is the cause.
    if result.denominator:
        assert result.denominator.exceeds_top_contributor is False
    assert result.warnings == []


def test_drivers_module_is_pure():
    """drivers.py may only import pure modules (stdlib and app.kpis)."""
    tree = ast.parse(inspect.getsource(drivers))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert imported <= {"collections.abc", "dataclasses", "decimal", "app.kpis"}, imported
