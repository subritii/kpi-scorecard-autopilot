"""Checks that the synthetic data really contains the planted events.

KPI values here are hand-computed straight from the raw facts (not via
app.kpis), so they double as an independent reference for Phase 2.
"""

import importlib.util
from collections import Counter, defaultdict
from datetime import date

import pytest

from app.config import REPO_ROOT

spec = importlib.util.spec_from_file_location(
    "generate_synthetic", REPO_ROOT / "scripts" / "generate_synthetic.py"
)
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)


@pytest.fixture(scope="module")
def data():
    return gen.generate()


def totals(data, client_id, metric_key) -> dict[str, float]:
    """week_start -> sum of a metric's facts for one client."""
    out = defaultdict(float)
    for f in data["source_facts"]:
        if f["client_id"] == client_id and f["metric_key"] == metric_key:
            out[f["week_start"]] += f["value"]
    return out


def by_dimension(data, client_id, metric_keys, week_start, dim) -> dict[str, float]:
    out = defaultdict(float)
    for f in data["source_facts"]:
        if (
            f["client_id"] == client_id
            and f["metric_key"] in metric_keys
            and f["week_start"] == week_start
        ):
            out[f["detail"][dim]] += f["value"]
    return out


def hand_kpis(data, client_id) -> dict[str, dict[str, float]]:
    """formula_key -> {week_start: value}, computed directly from facts."""
    rev = totals(data, client_id, "revenue")
    cogs = totals(data, client_id, "cogs")
    ar61 = totals(data, client_id, "ar_61_90")
    ar90 = totals(data, client_id, "ar_90_plus")
    kpis = {
        "weekly_revenue": dict(rev),
        "gross_margin_pct": {w: (rev[w] - cogs[w]) / rev[w] * 100 for w in rev},
        "cash_on_hand": dict(totals(data, client_id, "cash_ending")),
        "ar_over_60": {w: ar61[w] + ar90[w] for w in ar61},
    }
    bookings = totals(data, client_id, "bookings")
    if bookings:
        kpis["new_bookings"] = dict(bookings)
    inventory = totals(data, client_id, "inventory_balance")
    if inventory:
        kpis["inventory_days"] = {w: inventory[w] / (cogs[w] / 7) for w in inventory}
    return kpis


def status(value, target, direction, band_pct=5) -> str:
    miss = (target - value) if direction == "higher" else (value - target)
    if miss <= 0:
        return "green"
    return "yellow" if miss / target * 100 <= band_pct else "red"


def test_generation_is_deterministic():
    assert gen.generate() == gen.generate()


def test_shape(data):
    assert [c["id"] for c in data["clients"]] == ["brightline", "northgate"]
    assert len(data["kpi_definitions"]) == 10
    weeks = sorted({f["week_start"] for f in data["source_facts"]})
    assert len(weeks) == 12
    assert weeks[0] == "2026-07-06"
    assert all(date.fromisoformat(w).isoweekday() == 1 for w in weeks)


def test_ground_truth_lists_every_client_week_kpi(data):
    entries = data["ground_truth"]["expected_statuses"]
    keys = {(e["client_id"], e["formula_key"], e["week"]) for e in entries}
    assert len(entries) == len(keys) == 10 * 12
    counts = Counter(e["expected_status"] for e in entries)
    # AR 5-7 red + 8 yellow; margin 8 red; bookings 10 yellow + 11 red;
    # inventory 6-7 red + 8 yellow.
    assert counts == {"green": 110, "red": 7, "yellow": 3}


def test_every_client_week_matches_ground_truth(data):
    """Hand-computed status equals expected_status for all 120 client-week-KPIs."""
    expected = {
        (e["client_id"], e["formula_key"], e["week_start"]): e["expected_status"]
        for e in data["ground_truth"]["expected_statuses"]
    }
    checked = 0
    for k in data["kpi_definitions"]:
        values = hand_kpis(data, k["client_id"])[k["formula_key"]]
        for week_start, value in values.items():
            want = expected[(k["client_id"], k["formula_key"], week_start)]
            got = status(value, k["target"], k["direction"], k["yellow_band_pct"])
            assert got == want, (k["client_id"], k["formula_key"], week_start, value)
            checked += 1
    assert checked == 10 * 12


def test_planted_top_drivers(data):
    """In every event week, the planted cause is the biggest week-over-week mover.

    Ranked by size of change, so it covers both the spike and the wind-down
    (e.g. AR falling in week 8 is still driven by the two late payers).
    """
    weeks = data["ground_truth"]["week_starts"]
    metrics = {
        "ar_over_60": {"ar_61_90", "ar_90_plus"},
        "gross_margin_pct": {"cogs"},
        "new_bookings": {"bookings"},
        "inventory_days": {"inventory_balance"},
    }
    for event in data["ground_truth"]["events"]:
        for w in event["weeks"]:
            idx = weeks.index(w["week_start"])
            args = (data, event["client_id"], metrics[event["formula_key"]])
            now = by_dimension(*args, weeks[idx], event["driver_dimension"])
            before = by_dimension(*args, weeks[idx - 1], event["driver_dimension"])
            change = {k: abs(now[k] - before[k]) for k in now}
            n = len(event["top_drivers"])
            top = sorted(change, key=change.get, reverse=True)[:n]
            assert sorted(top) == sorted(event["top_drivers"]), (event["formula_key"], w)


def test_bookings_dip_is_mostly_implementation(data):
    """About 75% of the shortfall vs. normal bookings comes from Implementation."""
    normal = gen.BRIGHTLINE_BOOKINGS
    for w in data["ground_truth"]["week_starts"][9:11]:  # weeks 10-11
        actual = by_dimension(data, "brightline", {"bookings"}, w, "service_line")
        drop = {line: normal[line] - actual[line] for line in normal}
        share = drop["Implementation"] / sum(drop.values())
        assert 0.70 <= share <= 0.80, (w, share)


def test_contacts_use_reserved_example_domain(data):
    emails = [k["owner_email"] for k in data["kpi_definitions"]]
    for c in data["clients"]:
        emails += [c["controller_email"], *c["leadership_emails"]]
    assert all(e.endswith(".example") for e in emails)


def test_every_planted_pattern_has_last_years_explanation(data):
    past = {
        (d["client_id"], d["formula_key"])
        for d in data["docs"]
        if d["source_type"] == "past_explanation" and d["week_start"].startswith("2025-")
    }
    for event in data["ground_truth"]["events"]:
        assert (event["client_id"], event["formula_key"]) in past
