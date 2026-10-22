"""Driver analysis: which contributors moved a KPI week over week. Pure functions.

For each KPI we pick the underlying amount and the dimension to split it by
(e.g. AR over 60 by customer, COGS by account), then rank contributors by the
size of their change from the prior week. This says *what* moved, not *why*:
causes only come from the KPI owner's reply.

For ratio KPIs the contributors explain the amount on top of the ratio (COGS
accounts for gross margin, inventory categories for inventory days). The
denominator (revenue, COGS) is reported separately, with a flag when it moved
the KPI more than the top contributor did, so the wrong side isn't blamed.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from app.kpis import compute as compute_kpi
from app.kpis import metric_totals

UNASSIGNED = "Unassigned"

Row = tuple[str, Mapping, Decimal]  # (metric_key, detail_json, value)


@dataclass(frozen=True)
class WeekData:
    """One client-week of inputs: fact rows plus which source reports were loaded."""

    rows: tuple[Row, ...]
    loaded_sources: frozenset[str]


@dataclass(frozen=True)
class DriverSpec:
    metrics: frozenset[str]  # metric_keys summed per contributor
    dimension: str  # detail_json key naming the contributor


DRIVER_SPECS: dict[str, DriverSpec] = {
    "weekly_revenue": DriverSpec(frozenset({"revenue"}), "customer"),
    "gross_margin_pct": DriverSpec(frozenset({"cogs"}), "account"),
    "cash_on_hand": DriverSpec(frozenset({"cash_ending"}), "account"),
    "ar_over_60": DriverSpec(frozenset({"ar_61_90", "ar_90_plus"}), "customer"),
    "new_bookings": DriverSpec(frozenset({"bookings"}), "service_line"),
    "inventory_days": DriverSpec(frozenset({"inventory_balance"}), "category"),
}

# Ratio KPIs and the metric in their denominator.
DENOMINATORS = {"gross_margin_pct": "revenue", "inventory_days": "cogs"}


@dataclass(frozen=True)
class Driver:
    name: str
    prior: Decimal
    current: Decimal
    change: Decimal  # current - prior, signed
    # |change| / sum of all contributors' |change|: this contributor's share of
    # the week's total movement. Always 0..1 and sums to 1, even when increases
    # and decreases offset. None when nothing moved.
    share_of_movement: Decimal | None


@dataclass(frozen=True)
class DenominatorChange:
    metric: str
    prior: Decimal
    current: Decimal
    change: Decimal
    # Effects in the KPI's own units (e.g. margin points), each applied alone to
    # last week's numbers. Dollars aren't comparable across the two sides: a
    # COGS dollar moves inventory days ~5x more than an inventory dollar.
    effect_on_kpi: Decimal | None
    top_contributor_effect_on_kpi: Decimal | None
    exceeds_top_contributor: bool | None  # None when the effects can't be computed


@dataclass(frozen=True)
class DriverResult:
    formula_key: str
    drivers: list[Driver]
    net_change: Decimal | None  # change in the KPI itself; None if either week is no_data
    contributors_net_change: Decimal  # sum of contributor changes, in the amount's units
    denominator: DenominatorChange | None  # ratio KPIs only
    warnings: list[str] = field(default_factory=list)  # data-quality notes


def contributions(
    rows: Iterable[Row], metrics: frozenset[str], dimension: str
) -> tuple[dict[str, Decimal], int]:
    """Sum rows into {contributor: amount} for the given metrics.

    Rows missing the dimension are grouped under "Unassigned" (and counted) so
    their amount still shows up in the ranking instead of disappearing.
    """
    out: dict[str, Decimal] = {}
    missing = 0
    for metric_key, detail, value in rows:
        if metric_key not in metrics:
            continue
        name = detail.get(dimension)
        if name is None:
            name, missing = UNASSIGNED, missing + 1
        out[name] = out.get(name, Decimal(0)) + value
    return out, missing


def rank(
    current: Mapping[str, Decimal], prior: Mapping[str, Decimal], limit: int | None = None
) -> list[Driver]:
    """Rank contributors by size of week-over-week change, largest first.

    A contributor missing from one week counts as 0 there (a new customer is
    all increase, a lost one all decrease). Ties in size break by name, so the
    order never depends on input order.
    """
    for value in (*current.values(), *prior.values()):
        if not isinstance(value, Decimal):
            raise TypeError(f"expected Decimal, got {type(value).__name__}: {value!r}")

    names = set(current) | set(prior)
    changes = {n: current.get(n, Decimal(0)) - prior.get(n, Decimal(0)) for n in names}
    total_movement = sum((abs(c) for c in changes.values()), Decimal(0))

    ranked = [
        Driver(
            name=n,
            prior=prior.get(n, Decimal(0)),
            current=current.get(n, Decimal(0)),
            change=changes[n],
            share_of_movement=abs(changes[n]) / total_movement if total_movement else None,
        )
        for n in sorted(names, key=lambda n: (-abs(changes[n]), n))
    ]
    return ranked if limit is None else ranked[:limit]


def _totals(week: WeekData) -> dict[str, Decimal]:
    return metric_totals((metric_key, value) for metric_key, _, value in week.rows)


def _kpi_change(formula_key: str, current: WeekData, prior: WeekData) -> Decimal | None:
    now = compute_kpi(formula_key, _totals(current), current.loaded_sources)
    before = compute_kpi(formula_key, _totals(prior), prior.loaded_sources)
    return None if now is None or before is None else now - before


def _denominator_change(
    formula_key: str, current: WeekData, prior: WeekData, top: Driver | None
) -> DenominatorChange | None:
    metric = DENOMINATORS.get(formula_key)
    if metric is None:
        return None
    [contributor_metric] = DRIVER_SPECS[formula_key].metrics  # one metric for ratio KPIs
    prior_totals = _totals(prior)
    prior_den = prior_totals.get(metric, Decimal(0))
    current_den = _totals(current).get(metric, Decimal(0))

    def kpi_with(**changed: Decimal) -> Decimal | None:
        return compute_kpi(formula_key, {**prior_totals, **changed}, prior.loaded_sources)

    base = kpi_with()
    only_denominator = kpi_with(**{metric: current_den})
    top_change = top.change if top else Decimal(0)
    only_top = kpi_with(
        **{contributor_metric: prior_totals.get(contributor_metric, Decimal(0)) + top_change}
    )

    if base is None or only_denominator is None or only_top is None:
        den_effect = top_effect = exceeds = None
    else:
        den_effect, top_effect = only_denominator - base, only_top - base
        exceeds = abs(den_effect) > abs(top_effect)
    return DenominatorChange(
        metric=metric,
        prior=prior_den,
        current=current_den,
        change=current_den - prior_den,
        effect_on_kpi=den_effect,
        top_contributor_effect_on_kpi=top_effect,
        exceeds_top_contributor=exceeds,
    )


def drivers_for(
    formula_key: str, current: WeekData, prior: WeekData, limit: int | None = None
) -> DriverResult:
    """Ranked drivers, net change and (for ratios) the denominator, for one KPI-week.

    Raises KeyError for an unknown formula_key: that's a config error, not data.
    """
    spec = DRIVER_SPECS[formula_key]
    now, missing_now = contributions(current.rows, spec.metrics, spec.dimension)
    before, missing_before = contributions(prior.rows, spec.metrics, spec.dimension)
    ranked = rank(now, before)

    warnings = [
        f"{n} {label} row(s) had no {spec.dimension!r}; grouped as {UNASSIGNED}"
        for n, label in ((missing_now, "current-week"), (missing_before, "prior-week"))
        if n
    ]
    return DriverResult(
        formula_key=formula_key,
        drivers=ranked if limit is None else ranked[:limit],
        net_change=_kpi_change(formula_key, current, prior),
        contributors_net_change=sum((d.change for d in ranked), Decimal(0)),
        denominator=_denominator_change(formula_key, current, prior, ranked[0] if ranked else None),
        warnings=warnings,
    )
