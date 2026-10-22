"""Tests for app.kpis: hand-worked examples first, then the 120 ground-truth values."""

import ast
import csv
import inspect
import json
from collections import Counter, defaultdict
from decimal import Decimal as D

import pytest

from app import kpis
from app.config import REPO_ROOT

ALL_SOURCES = {"qbo_profit_and_loss", "qbo_balance_sheet", "qbo_ar_aging", "sales_sheet"}


def totals(**metrics) -> dict:
    """Shorthand for metric totals, e.g. totals(revenue="1000", cogs="580")."""
    return {k: D(v) for k, v in metrics.items()}


def compute(formula_key, loaded=ALL_SOURCES, **metrics):
    """compute() with every source loaded unless the test says otherwise."""
    return kpis.compute(formula_key, totals(**metrics), loaded)


# --- metric_totals -----------------------------------------------------------


def test_metric_totals_sums_rows_by_metric():
    rows = [("revenue", D("100.10")), ("revenue", D("200.20")), ("cogs", D("50"))]
    assert kpis.metric_totals(rows) == {"revenue": D("300.30"), "cogs": D("50")}


def test_metric_totals_rejects_float():
    with pytest.raises(TypeError):
        kpis.metric_totals([("revenue", 100.1)])


# --- formulas ----------------------------------------------------------------


def test_weekly_revenue_is_exact_sum():
    # 0.1 + 0.2 is exact with Decimal (it would be 0.30000000000000004 as float).
    rows = [("revenue", D("0.1")), ("revenue", D("0.2"))]
    assert kpis.compute("weekly_revenue", kpis.metric_totals(rows), ALL_SOURCES) == D("0.3")


def test_gross_margin_pct_hand_worked():
    # (1000 - 580) / 1000 * 100 = 42
    assert compute("gross_margin_pct", revenue="1000", cogs="580") == D("42")


def test_gross_margin_pct_is_not_rounded():
    # (3 - 2) / 3 * 100 = 33.333...; rounding is for display only.
    value = compute("gross_margin_pct", revenue="3", cogs="2")
    assert D("33.333") < value < D("33.334")
    assert kpis.round_for_display("gross_margin_pct", value) == D("33.3")


def test_gross_margin_pct_zero_revenue_is_none():
    assert compute("gross_margin_pct", revenue="0", cogs="10") is None
    assert compute("gross_margin_pct", cogs="10") is None  # P&L loaded, no revenue rows


def test_gross_margin_pct_no_cogs_rows_is_100():
    # P&L loaded with revenue but no COGS rows: COGS really was 0.
    assert compute("gross_margin_pct", revenue="1000") == D("100")


def test_cash_on_hand_sums_accounts():
    rows = [("cash_ending", D("470000.00")), ("cash_ending", D("70000.50"))]
    assert kpis.compute("cash_on_hand", kpis.metric_totals(rows), ALL_SOURCES) == D("540000.50")


def test_ar_over_60_adds_both_buckets():
    assert compute("ar_over_60", ar_61_90="30000", ar_90_plus="12500") == D("42500")


def test_ar_over_60_with_one_bucket_present():
    # No 90+ rows that week just means nothing is that old yet.
    assert compute("ar_over_60", ar_61_90="30000") == D("30000")


def test_new_bookings_sum():
    assert compute("new_bookings", bookings="195000") == D("195000")


def test_inventory_days_hand_worked():
    # 70,000 / (10,000 / 7) = 49 days
    assert compute("inventory_days", inventory_balance="70000", cogs="10000") == D("49")


def test_inventory_days_non_terminating():
    # 100 / (21 / 7) = 33.333...
    value = compute("inventory_days", inventory_balance="100", cogs="21")
    assert kpis.round_for_display("inventory_days", value) == D("33.3")


def test_inventory_days_zero_cogs_is_none():
    assert compute("inventory_days", inventory_balance="70000", cogs="0") is None
    assert compute("inventory_days", inventory_balance="70000") is None


def test_unknown_formula_key_raises():
    with pytest.raises(KeyError):
        compute("ebitda", revenue="1")


# --- loaded-but-empty vs. not loaded -------------------------------------------


def test_ar_report_loaded_but_empty_is_zero_and_green():
    # The AR aging report arrived and had no 61+ rows: AR over 60 really is 0.
    value = compute("ar_over_60", loaded={"qbo_ar_aging"})
    assert value == D("0")
    assert kpis.status(value, D("90000"), "lower") == "green"


def test_ar_report_not_loaded_is_no_data():
    value = compute("ar_over_60", loaded={"qbo_profit_and_loss", "qbo_balance_sheet"})
    assert value is None
    assert kpis.status(value, D("90000"), "lower") == "no_data"


def test_sales_sheet_loaded_but_empty_is_zero_and_red():
    # The sheet arrived with no bookings: that is a real 0, and a red KPI.
    value = compute("new_bookings", loaded={"sales_sheet"})
    assert value == D("0")
    assert kpis.status(value, D("170000"), "higher") == "red"


def test_sales_sheet_not_loaded_is_no_data():
    value = compute("new_bookings", loaded={"qbo_profit_and_loss"})
    assert value is None
    assert kpis.status(value, D("170000"), "higher") == "no_data"


def test_facts_without_a_load_record_are_not_trusted():
    # Rows exist but the load was never recorded (e.g. a pull failed halfway):
    # without the record we can't tell the data is complete, so no_data.
    assert compute("ar_over_60", loaded=set(), ar_61_90="30000") is None


def test_inventory_days_needs_both_reports():
    m = {"inventory_balance": "70000", "cogs": "10000"}
    assert compute("inventory_days", loaded={"qbo_balance_sheet"}, **m) is None
    assert compute("inventory_days", loaded={"qbo_profit_and_loss"}, **m) is None


# --- status --------------------------------------------------------------------


# Higher is better: target 42, band 5% of 42 = 2.1 points, so yellow down to 39.9.
@pytest.mark.parametrize(
    "value, expected",
    [
        ("43", "green"),
        ("42", "green"),  # exactly at target
        ("41.9999", "yellow"),
        ("39.9", "yellow"),  # exactly at the yellow-band edge
        ("39.8999", "red"),
        ("0", "red"),
    ],
)
def test_status_higher_is_better(value, expected):
    assert kpis.status(D(value), D("42"), "higher", D("5")) == expected


# Lower is better: target 90,000, band 5% = 4,500, so yellow up to 94,500.
@pytest.mark.parametrize(
    "value, expected",
    [
        ("55000", "green"),
        ("90000", "green"),  # exactly at target
        ("90000.01", "yellow"),
        ("94500", "yellow"),  # exactly at the yellow-band edge
        ("94500.01", "red"),
    ],
)
def test_status_lower_is_better(value, expected):
    assert kpis.status(D(value), D("90000"), "lower", D("5")) == expected


def test_status_uses_unrounded_value():
    # 41.99 displays as 42.0 but is below target, so it is yellow, not green.
    value = compute("gross_margin_pct", revenue="10000", cogs="5801")
    assert kpis.round_for_display("gross_margin_pct", value) == D("42.0")
    assert kpis.status(value, D("42"), "higher", D("5")) == "yellow"


def test_status_band_is_relative_to_target():
    # Same 2-unit miss: yellow against target 100 (band 5), red against target 10 (band 0.5).
    assert kpis.status(D("98"), D("100"), "higher", D("5")) == "yellow"
    assert kpis.status(D("8"), D("10"), "higher", D("5")) == "red"


def test_status_custom_band():
    assert kpis.status(D("37"), D("42"), "higher", D("12")) == "yellow"  # band 5.04 points


def test_status_none_is_no_data_never_green():
    assert kpis.status(None, D("42"), "higher", D("5")) == "no_data"
    assert kpis.status(None, D("90000"), "lower", D("5")) == "no_data"


def test_status_rejects_float():
    with pytest.raises(TypeError):
        kpis.status(41.9, D("42"), "higher", D("5"))


def test_status_rejects_unknown_direction():
    with pytest.raises(ValueError):
        kpis.status(D("1"), D("1"), "up", D("5"))


# --- display rounding ----------------------------------------------------------


def test_round_for_display_half_up():
    # Half-up, not banker's rounding: 42.45 -> 42.5 (banker's would give 42.4).
    assert kpis.round_for_display("gross_margin_pct", D("42.45")) == D("42.5")
    assert kpis.round_for_display("weekly_revenue", D("171418.125")) == D("171418.13")
    assert kpis.round_for_display("gross_margin_pct", None) is None


# --- against the 120 ground-truth values ----------------------------------------


def test_all_120_ground_truth_statuses():
    seed = REPO_ROOT / "data" / "seed"
    definitions = json.loads((seed / "kpi_definitions.json").read_text())
    ground_truth = json.loads((seed / "ground_truth.json").read_text())

    rows = defaultdict(list)  # (client_id, week_start) -> [(metric_key, value)]
    with open(seed / "source_facts.csv", newline="") as f:
        for r in csv.DictReader(f):
            rows[(r["client_id"], r["week_start"])].append((r["metric_key"], D(r["value"])))
    loaded = defaultdict(set)  # (client_id, week_start) -> {source}
    with open(seed / "source_loads.csv", newline="") as f:
        for r in csv.DictReader(f):
            loaded[(r["client_id"], r["week_start"])].add(r["source"])
    by_kpi = {(d["client_id"], d["formula_key"]): d for d in definitions}

    results = Counter()
    for e in ground_truth["expected_statuses"]:
        key = (e["client_id"], e["week_start"])
        d = by_kpi[(e["client_id"], e["formula_key"])]
        value = kpis.compute(e["formula_key"], kpis.metric_totals(rows[key]), loaded[key])
        got = kpis.status(value, D(str(d["target"])), d["direction"], D(str(d["yellow_band_pct"])))
        assert got == e["expected_status"], (e, value)
        results[got] += 1
    assert results == {"green": 110, "red": 7, "yellow": 3}


def test_kpis_module_is_pure():
    """kpis.py may only import pure standard-library modules (no DB, network, or LLM)."""
    tree = ast.parse(inspect.getsource(kpis))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert imported <= {"collections.abc", "decimal", "typing"}, imported
