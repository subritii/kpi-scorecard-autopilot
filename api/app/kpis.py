"""KPI formulas and status logic. Pure functions: no database, network, or LLM.

All numbers are Decimal (never float), so money sums are exact and results
are reproducible. Values are kept unrounded; status is decided on the exact
value and rounding happens only in round_for_display().

Typical use for one client-week:
    totals = metric_totals([(metric_key, value), ...])           # from source_facts
    loaded = {"qbo_profit_and_loss", "qbo_ar_aging", ...}        # from source_loads
    value = compute("gross_margin_pct", totals, loaded)          # Decimal or None
    status(value, target, direction, yellow_band_pct)            # green/yellow/red/no_data
"""

from collections.abc import Callable, Collection, Iterable, Mapping
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

Status = Literal["green", "yellow", "red", "no_data"]
DEFAULT_YELLOW_BAND_PCT = Decimal("5")

# Display precision per formula; everything else is money shown to the cent.
DISPLAY_PLACES = {"gross_margin_pct": 1, "inventory_days": 1}

# Source reports each KPI needs (names match source_loads.source). If any is
# not loaded for the week, the KPI is None -> no_data. If it was loaded, a
# metric with no rows is a real 0 (e.g. no AR older than 60 days).
REQUIRED_SOURCES: dict[str, frozenset[str]] = {
    "weekly_revenue": frozenset({"qbo_profit_and_loss"}),
    "gross_margin_pct": frozenset({"qbo_profit_and_loss"}),
    "cash_on_hand": frozenset({"qbo_balance_sheet"}),
    "ar_over_60": frozenset({"qbo_ar_aging"}),
    "new_bookings": frozenset({"sales_sheet"}),
    "inventory_days": frozenset({"qbo_balance_sheet", "qbo_profit_and_loss"}),  # inventory, cogs
}


def _require_decimal(*values) -> None:
    # Floats can't represent most cents exactly (0.1 + 0.2 != 0.3), so they are refused.
    for v in values:
        if not isinstance(v, Decimal):
            raise TypeError(f"expected Decimal, got {type(v).__name__}: {v!r}")


def metric_totals(rows: Iterable[tuple[str, Decimal]]) -> dict[str, Decimal]:
    """Sum (metric_key, value) rows into one total per metric_key."""
    out: dict[str, Decimal] = {}
    for metric_key, value in rows:
        _require_decimal(value)
        out[metric_key] = out.get(metric_key, Decimal(0)) + value
    return out


# --- formulas ----------------------------------------------------------------
# Each receives `m`, a lookup that returns a metric's weekly total (0 when the
# loaded report had no rows for it). They return None only when the formula
# is undefined, i.e. division by zero.

Metric = Callable[[str], Decimal]


def _weekly_revenue(m: Metric) -> Decimal | None:
    return m("revenue")


def _gross_margin_pct(m: Metric) -> Decimal | None:
    revenue = m("revenue")
    if revenue == 0:
        return None
    return (revenue - m("cogs")) / revenue * 100


def _cash_on_hand(m: Metric) -> Decimal | None:
    return m("cash_ending")


def _ar_over_60(m: Metric) -> Decimal | None:
    return m("ar_61_90") + m("ar_90_plus")


def _new_bookings(m: Metric) -> Decimal | None:
    return m("bookings")


def _inventory_days(m: Metric) -> Decimal | None:
    cogs = m("cogs")
    if cogs == 0:
        return None
    # inventory / (cogs / 7), written as inventory * 7 / cogs so there is one
    # division instead of two (no intermediate rounding of cogs / 7).
    return m("inventory_balance") * 7 / cogs


FORMULAS: dict[str, Callable[[Metric], Decimal | None]] = {
    "weekly_revenue": _weekly_revenue,
    "gross_margin_pct": _gross_margin_pct,
    "cash_on_hand": _cash_on_hand,
    "ar_over_60": _ar_over_60,
    "new_bookings": _new_bookings,
    "inventory_days": _inventory_days,
}


def compute(
    formula_key: str, totals: Mapping[str, Decimal], loaded_sources: Collection[str]
) -> Decimal | None:
    """Unrounded KPI value for one client-week, or None if it can't be computed.

    None when a required source report wasn't loaded that week (even if stray
    facts exist, since the load record is what says the data is complete), or
    when the formula divides by zero. Raises KeyError for an unknown formula.
    """
    formula = FORMULAS[formula_key]
    if not REQUIRED_SOURCES[formula_key] <= set(loaded_sources):
        return None
    return formula(lambda metric_key: totals.get(metric_key, Decimal(0)))


# --- status --------------------------------------------------------------------


def status(
    value: Decimal | None,
    target: Decimal,
    direction: str,
    yellow_band_pct: Decimal = DEFAULT_YELLOW_BAND_PCT,
) -> Status:
    """green: meets target; yellow: misses by at most the band; red: misses by more.

    Boundaries: exactly at target is green; exactly at the band edge is yellow.
    The band is relative to the target, not absolute points: a 5% band on a
    42% margin target allows a 2.1-point miss (yellow down to 39.9), and on a
    $90K AR target allows $4,500 (yellow up to $94,500).
    """
    if direction not in ("higher", "lower"):
        raise ValueError(f"direction must be 'higher' or 'lower', got {direction!r}")
    if value is None:
        return "no_data"
    _require_decimal(value, target, yellow_band_pct)

    # How far the value falls short of the target, in the KPI's own units (<= 0 means met).
    shortfall = target - value if direction == "higher" else value - target
    if shortfall <= 0:
        return "green"
    allowed_miss = abs(target) * yellow_band_pct / 100
    return "yellow" if shortfall <= allowed_miss else "red"


# --- display -------------------------------------------------------------------


def round_for_display(formula_key: str, value: Decimal | None) -> Decimal | None:
    """Round half-up for people to read (1 decimal for % and days, cents for money)."""
    if value is None:
        return None
    places = DISPLAY_PLACES.get(formula_key, 2)
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
