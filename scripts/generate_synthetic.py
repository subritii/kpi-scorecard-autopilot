"""Generate synthetic data: 2 fictional clients x 12 weeks, with planted events.

Writes to data/seed/:
  clients.json          client records
  kpi_definitions.json  KPIs, owners, targets
  source_facts.csv      raw weekly numbers, one row per metric per dimension
  source_loads.csv      which source reports were loaded per client-week (+ row count)
  docs.json             KPI definitions, notes, and last year's explanations (for RAG)
  ground_truth.json     expected status for all 120 client-week-KPIs, plus the
                        planted events and their expected top drivers (for the eval)

A fixed seed makes every run produce identical output. All companies and
people are invented; emails use the reserved .example domain.

Noise ranges and planted amounts are sized so that normal weeks always stay
green and planted weeks always land in the intended status, whatever the
random draws are (tests/test_synthetic_data.py checks this). Where the yellow
band is too narrow for that (e.g. inventory days 45-47.25), the planted amount
is solved from the week's other numbers so the KPI lands on a chosen level.
"""

import csv
import json
import random
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

SEED = 42
FIRST_WEEK = date(2026, 7, 6)  # a Monday
NUM_WEEKS = 12
OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "seed"

FIRM_CONTROLLER = "controller@ledgerline-cfo.example"

KPI_DEFINITIONS = {
    "weekly_revenue": "Total revenue recognized in the week (Mon-Sun), summed across customers.",
    "gross_margin_pct": "(Revenue - cost of goods sold) / revenue for the week, as a percentage "
    "rounded to 1 decimal.",
    "cash_on_hand": "Ending cash balance across all bank accounts at the end of the week.",
    "ar_over_60": "Accounts receivable more than 60 days past invoice date "
    "(61-90 and 90+ aging buckets).",
    "new_bookings": "Value of new signed contracts in the week, from the sales bookings sheet.",
    "inventory_days": "Inventory balance / average daily COGS (weekly COGS / 7).",
}

KPI_NAMES = {
    "weekly_revenue": "Weekly Revenue",
    "gross_margin_pct": "Gross Margin %",
    "cash_on_hand": "Cash on Hand",
    "ar_over_60": "AR Over 60 Days",
    "new_bookings": "New Bookings",
    "inventory_days": "Inventory Days",
}

# ---------------------------------------------------------------- Brightline
# ~$8-9M services company. Customer -> share of weekly revenue (sums to 1).
BRIGHTLINE_CUSTOMERS = {
    "Ambercrest Logistics": 0.17,
    "Larkspur Clinics": 0.15,
    "Quillfield Dental Group": 0.14,
    "Orchard Vale Architects": 0.13,
    "Pinecastle Analytics": 0.12,
    "Mosswood Hospitality": 0.11,
    "Redwick Learning": 0.10,
    "Harlow Bay Insurance Brokers": 0.08,
}
BRIGHTLINE_REVENUE = 172_000
# COGS account -> share of revenue. Total 0.53, so gross margin is about 47%.
BRIGHTLINE_COGS_RATES = {
    "Direct labor": 0.33,
    "Subcontractor fees": 0.12,
    "Software subscriptions": 0.05,
    "Project travel": 0.03,
}
BRIGHTLINE_CASH = {"Operating checking": 470_000, "Payroll reserve": 70_000}
BRIGHTLINE_BOOKINGS = {"Advisory": 70_000, "Implementation": 85_000, "Managed services": 40_000}

# Planted: weeks 5-7, two customers stop paying. Extra (61-90, 90+) AR per
# week; the balance ages from the 61-90 bucket into 90+ over time.
LATE_PAYERS = {
    "Ambercrest Logistics": {5: (34_000, 0), 6: (18_000, 30_000), 7: (12_000, 48_000)},
    "Larkspur Clinics": {5: (27_000, 0), 6: (14_000, 25_000), 7: (9_000, 38_000)},
}
# Week 8 wind-down: Ambercrest has paid in full, Larkspur only partly. Larkspur's
# remaining 90+ balance is solved so total AR over 60 lands at this level, inside
# the yellow band (target 90K, yellow up to 94.5K). Week 9 is back to normal.
AR_WIND_DOWN_WEEK = 8
AR_WIND_DOWN_CUSTOMER = "Larkspur Clinics"
AR_WIND_DOWN_TOTAL = 92_250
# Planted: week 8, the staffing partner raises rates -> subcontractor fees double.
PRICE_INCREASE_WEEK = 8
PRICE_INCREASE_ACCOUNT = "Subcontractor fees"
PRICE_INCREASE_RATE = 0.24
# Planted: weeks 10-11, seasonal dip. Total bookings for the week (yellow band
# is 161.5K-170K, then clearly red). 75% of the shortfall from normal comes out
# of Implementation; the other lines share the remaining 25% pro rata.
BOOKINGS_DIP_TOTAL = {10: 165_000, 11: 136_500}
BOOKINGS_DIP_LINE = "Implementation"
BOOKINGS_DIP_LINE_SHARE = 0.75

# ----------------------------------------------------------------- Northgate
# ~$12-13M distributor.
NORTHGATE_CUSTOMERS = {
    "Kestrel Home Supply": 0.18,
    "Ironbridge Contractors": 0.16,
    "Valemont Hardware": 0.14,
    "Bluepine Builders": 0.13,
    "Saltmarsh Fixtures": 0.11,
    "Copperfield Renovations": 0.10,
    "Thornbury Facilities": 0.10,
    "Westerly Plumbing Co": 0.08,
}
NORTHGATE_REVENUE = 255_000
NORTHGATE_COGS_RATES = {"Product cost": 0.655, "Freight-in": 0.03}  # margin about 31.5%
NORTHGATE_CASH = {"Operating checking": 690_000, "Reserve account": 110_000}
# Inventory by category, about 38 days of COGS in total.
NORTHGATE_INVENTORY = {
    "Plumbing fixtures": 280_000,
    "Electrical": 240_000,
    "Fasteners & hardware": 190_000,
    "HVAC parts": 238_000,
}
# Planted: week 6, an overstock purchase of plumbing fixtures, sold down over
# the next two weeks. Inventory days per week (target 45, yellow up to 47.25):
# the plumbing balance is solved so total inventory hits this level given that
# week's COGS. Week 9 is back to normal.
OVERSTOCK_CATEGORY = "Plumbing fixtures"
OVERSTOCK_DAYS = {6: 55.0, 7: 50.0, 8: 46.5}

# ------------------------------------------------------------- Source reports
# Which report each metric comes from (names match source_loads.source).
METRIC_SOURCE = {
    "revenue": "qbo_profit_and_loss",
    "cogs": "qbo_profit_and_loss",
    "cash_ending": "qbo_balance_sheet",
    "inventory_balance": "qbo_balance_sheet",
    "ar_61_90": "qbo_ar_aging",
    "ar_90_plus": "qbo_ar_aging",
    "bookings": "sales_sheet",
}
# Reports pulled for each client every week. Every report is loaded every
# week in this dataset; a missing report would make its KPIs no_data.
CLIENT_SOURCES = {
    "brightline": ["qbo_profit_and_loss", "qbo_balance_sheet", "qbo_ar_aging", "sales_sheet"],
    "northgate": ["qbo_profit_and_loss", "qbo_balance_sheet", "qbo_ar_aging"],
}


def week_starts() -> list[date]:
    return [FIRST_WEEK + timedelta(weeks=i) for i in range(NUM_WEEKS)]


def jitter(rng: random.Random, pct: float) -> float:
    """Random multiplier in [1 - pct, 1 + pct]."""
    return rng.uniform(1 - pct, 1 + pct)


def fact(client_id: str, week_start: date, metric_key: str, value: float, **detail) -> dict:
    return {
        "client_id": client_id,
        "week_start": week_start.isoformat(),
        "metric_key": metric_key,
        "value": round(value, 2),
        "detail": detail,
    }


def ar_rows(client_id, week_start, customer, bucket_61_90, bucket_90_plus) -> list[dict]:
    return [
        fact(client_id, week_start, "ar_61_90", bucket_61_90, customer=customer),
        fact(client_id, week_start, "ar_90_plus", bucket_90_plus, customer=customer),
    ]


def brightline_week(rng: random.Random, week: int, ws: date) -> list[dict]:
    c = "brightline"
    rows = []

    revenue = {
        name: BRIGHTLINE_REVENUE * share * jitter(rng, 0.03)
        for name, share in BRIGHTLINE_CUSTOMERS.items()
    }
    rows += [fact(c, ws, "revenue", v, customer=name) for name, v in revenue.items()]
    total_revenue = sum(revenue.values())

    for account, rate in BRIGHTLINE_COGS_RATES.items():
        if week == PRICE_INCREASE_WEEK and account == PRICE_INCREASE_ACCOUNT:
            week_rate = PRICE_INCREASE_RATE * jitter(rng, 0.02)
        else:
            week_rate = rate * jitter(rng, 0.05)
        rows.append(fact(c, ws, "cogs", total_revenue * week_rate, account=account))

    for account, balance in BRIGHTLINE_CASH.items():
        rows.append(fact(c, ws, "cash_ending", balance * jitter(rng, 0.04), account=account))

    # AR: normal (61-90, 90+) buckets per customer, then the planted extras.
    ar = {
        customer: [4_500 * jitter(rng, 0.15), 2_400 * jitter(rng, 0.15)]
        for customer in BRIGHTLINE_CUSTOMERS
    }
    for customer, by_week in LATE_PAYERS.items():
        extra61, extra90 = by_week.get(week, (0, 0))
        ar[customer][0] += extra61
        ar[customer][1] += extra90
    if week == AR_WIND_DOWN_WEEK:
        normal_total = sum(b61 + b90 for b61, b90 in ar.values())
        ar[AR_WIND_DOWN_CUSTOMER][1] += AR_WIND_DOWN_TOTAL * jitter(rng, 0.005) - normal_total
    for customer, (b61, b90) in ar.items():
        rows += ar_rows(c, ws, customer, b61, b90)

    dip_total = BOOKINGS_DIP_TOTAL.get(week)
    if dip_total:
        shortfall = sum(BRIGHTLINE_BOOKINGS.values()) - dip_total
        others = sum(v for k, v in BRIGHTLINE_BOOKINGS.items() if k != BOOKINGS_DIP_LINE)
    for line, amount in BRIGHTLINE_BOOKINGS.items():
        if not dip_total:
            value = amount * jitter(rng, 0.05)
        elif line == BOOKINGS_DIP_LINE:
            value = (amount - shortfall * BOOKINGS_DIP_LINE_SHARE) * jitter(rng, 0.01)
        else:
            share_of_rest = amount / others
            value = (amount - shortfall * (1 - BOOKINGS_DIP_LINE_SHARE) * share_of_rest) * jitter(
                rng, 0.01
            )
        rows.append(fact(c, ws, "bookings", value, service_line=line))

    return rows


def northgate_week(rng: random.Random, week: int, ws: date) -> list[dict]:
    c = "northgate"
    rows = []

    revenue = {
        name: NORTHGATE_REVENUE * share * jitter(rng, 0.03)
        for name, share in NORTHGATE_CUSTOMERS.items()
    }
    rows += [fact(c, ws, "revenue", v, customer=name) for name, v in revenue.items()]
    total_revenue = sum(revenue.values())

    cogs = {
        "Product cost": total_revenue * 0.655 * jitter(rng, 0.02),
        "Freight-in": total_revenue * 0.03 * jitter(rng, 0.05),
    }
    rows += [fact(c, ws, "cogs", v, account=account) for account, v in cogs.items()]

    for account, balance in NORTHGATE_CASH.items():
        rows.append(fact(c, ws, "cash_ending", balance * jitter(rng, 0.04), account=account))

    for customer in NORTHGATE_CUSTOMERS:
        rows += ar_rows(c, ws, customer, 8_000 * jitter(rng, 0.15), 4_000 * jitter(rng, 0.15))

    inventory = {
        category: balance * jitter(rng, 0.03) for category, balance in NORTHGATE_INVENTORY.items()
    }
    if week in OVERSTOCK_DAYS:
        # inventory_days = total inventory / (weekly COGS / 7), so solve for the
        # plumbing balance that makes the total hit the planted level.
        target_total = OVERSTOCK_DAYS[week] * jitter(rng, 0.003) * sum(cogs.values()) / 7
        others = sum(v for k, v in inventory.items() if k != OVERSTOCK_CATEGORY)
        inventory[OVERSTOCK_CATEGORY] = target_total - others
    rows += [
        fact(c, ws, "inventory_balance", v, category=category) for category, v in inventory.items()
    ]

    return rows


def build_clients() -> list[dict]:
    return [
        {
            "id": "brightline",
            "name": "Brightline Services",
            "controller_email": FIRM_CONTROLLER,
            "leadership_emails": ["ceo@brightline.example", "coo@brightline.example"],
            "clickup_list_id": None,  # set in Phase 6
        },
        {
            "id": "northgate",
            "name": "Northgate Supply",
            "controller_email": FIRM_CONTROLLER,
            "leadership_emails": ["ceo@northgate.example", "cfo@northgate.example"],
            "clickup_list_id": None,
        },
    ]


def kpi(client_id, formula_key, owner_name, target, direction, source="quickbooks") -> dict:
    domain = f"{client_id}.example"
    return {
        "client_id": client_id,
        "name": KPI_NAMES[formula_key],
        "definition": KPI_DEFINITIONS[formula_key],
        "owner_name": owner_name,
        "owner_email": owner_name.lower().replace(" ", ".") + "@" + domain,
        "source": source,
        "formula_key": formula_key,
        "target": target,
        "direction": direction,
        "yellow_band_pct": 5,
    }


def build_kpi_definitions() -> list[dict]:
    return [
        kpi("brightline", "weekly_revenue", "Sam Keller", 155_000, "higher"),
        kpi("brightline", "gross_margin_pct", "Lena Brooks", 42, "higher"),
        kpi("brightline", "cash_on_hand", "Avery Chen", 400_000, "higher"),
        kpi("brightline", "ar_over_60", "Avery Chen", 90_000, "lower"),
        kpi("brightline", "new_bookings", "Marcus Hale", 170_000, "higher", source="sales_sheet"),
        kpi("northgate", "weekly_revenue", "Rosa Delgado", 230_000, "higher"),
        kpi("northgate", "gross_margin_pct", "Rosa Delgado", 28, "higher"),
        kpi("northgate", "cash_on_hand", "Ben Okafor", 600_000, "higher"),
        kpi("northgate", "ar_over_60", "Ben Okafor", 150_000, "lower"),
        kpi("northgate", "inventory_days", "Tom Lindqvist", 45, "lower"),
    ]


def build_docs(kpis: list[dict]) -> list[dict]:
    """Documents for retrieval. Past explanations are last year's owner replies."""
    docs = [
        {
            "client_id": k["client_id"],
            "formula_key": k["formula_key"],
            "week_start": None,
            "source_type": "definition",
            "text": f"{k['name']}: {k['definition']} Owner: {k['owner_name']}.",
        }
        for k in kpis
    ]

    def doc(client_id, formula_key, week_start, source_type, text):
        return {
            "client_id": client_id,
            "formula_key": formula_key,
            "week_start": week_start,
            "source_type": source_type,
            "text": text,
        }

    docs += [
        doc(
            "brightline",
            None,
            None,
            "note",
            "Brightline bills monthly on net-30 terms. Collections follow-up starts at 45 days.",
        ),
        doc(
            "brightline",
            "gross_margin_pct",
            None,
            "note",
            "Subcontractor fees are billed by an outside staffing partner under a rate card "
            "that is reviewed each summer.",
        ),
        doc(
            "brightline",
            "ar_over_60",
            "2025-08-04",
            "past_explanation",
            "What changed: AR over 60 days rose well above target for three weeks, then eased "
            "back as the balances were paid down. "
            "Why: Ambercrest Logistics and Mosswood Hospitality held payments while moving to "
            "a new AP system. Owner of fix: Avery Chen. Actions: weekly calls with both AP "
            "teams; one paid in full, the other in two installments.",
        ),
        doc(
            "brightline",
            "gross_margin_pct",
            "2025-08-25",
            "past_explanation",
            "What changed: gross margin dropped below target for one week. "
            "Why: the staffing partner applied its new rate card mid-contract, so subcontractor "
            "fees jumped. Owner of fix: Lena Brooks. Actions: renegotiated to honor the old rate "
            "through contract end; added rate-card review to the summer calendar.",
        ),
        doc(
            "brightline",
            "new_bookings",
            "2025-09-15",
            "past_explanation",
            "What changed: new bookings fell for two weeks in September. "
            "Why: late-summer slowdown, mostly in Implementation projects; prospects pushed "
            "signing decisions until their new budget cycle. Owner of fix: Marcus Hale. "
            "Actions: front-loaded Implementation proposals in August; pipeline recovered "
            "by October.",
        ),
        doc(
            "brightline",
            "cash_on_hand",
            "2025-06-30",
            "past_explanation",
            "What changed: cash dipped but stayed above target. "
            "Why: annual insurance premium was prepaid. Owner of fix: Avery Chen. "
            "Actions: none needed; planned outflow.",
        ),
        doc(
            "northgate",
            None,
            None,
            "note",
            "Northgate purchasing reviews open purchase orders every Thursday.",
        ),
        doc(
            "northgate",
            "gross_margin_pct",
            None,
            "note",
            "Northgate gross margin is sensitive to freight-in costs during peak season.",
        ),
        doc(
            "northgate",
            "inventory_days",
            "2025-09-22",
            "past_explanation",
            "What changed: inventory days spiked above target, then came down over the next "
            "two weeks. "
            "Why: purchasing placed an early bulk order of plumbing fixtures ahead of an "
            "announced supplier price increase. Owner of fix: Tom Lindqvist. Actions: sold "
            "through the excess; added a second approval for large POs.",
        ),
        # Same KPI as a Brightline explanation, on purpose: retrieval for
        # Brightline AR must never return this Northgate document.
        doc(
            "northgate",
            "ar_over_60",
            "2025-08-11",
            "past_explanation",
            "What changed: AR over 60 days rose. "
            "Why: Valemont Hardware disputed a damaged shipment and held payment until a "
            "credit memo was issued. Owner of fix: Ben Okafor. Actions: issued the credit "
            "memo; balance paid the following week.",
        ),
    ]
    # Stable key per document (source type, KPI, week), so embed_docs.py can
    # upsert on it and re-runs never create duplicates.
    for d in docs:
        d["doc_key"] = ":".join(
            [d["source_type"], d["formula_key"] or "general", d["week_start"] or "undated"]
        )
    keys = [(d["client_id"], d["doc_key"]) for d in docs]
    assert len(keys) == len(set(keys)), "doc_key must be unique per client"
    return docs


# The planted events: which weeks each one affects, the status it should
# produce, and the expected top driver(s) of the week-over-week change.
EVENTS = [
    {
        "client_id": "brightline",
        "formula_key": "ar_over_60",
        "statuses": {5: "red", 6: "red", 7: "red", AR_WIND_DOWN_WEEK: "yellow"},
        "top_drivers": sorted(LATE_PAYERS),
        "driver_dimension": "customer",
        "description": "Two customers pay late; balances age from 61-90 into 90+, "
        "then one pays in full and the other partly in week 8.",
    },
    {
        "client_id": "brightline",
        "formula_key": "gross_margin_pct",
        "statuses": {PRICE_INCREASE_WEEK: "red"},
        "top_drivers": [PRICE_INCREASE_ACCOUNT],
        "driver_dimension": "account",
        "description": "Supplier price increase doubles subcontractor fees for one week.",
    },
    {
        "client_id": "brightline",
        "formula_key": "new_bookings",
        "statuses": {10: "yellow", 11: "red"},
        "top_drivers": [BOOKINGS_DIP_LINE],
        "driver_dimension": "service_line",
        "description": "Seasonal dip in new bookings, mostly in Implementation.",
    },
    {
        "client_id": "northgate",
        "formula_key": "inventory_days",
        "statuses": {6: "red", 7: "red", 8: "yellow"},
        "top_drivers": [OVERSTOCK_CATEGORY],
        "driver_dimension": "category",
        "description": "Overstock purchase of plumbing fixtures, sold down over two weeks.",
    },
]


def build_ground_truth(weeks: list[date], kpis: list[dict]) -> dict:
    planted = {
        (e["client_id"], e["formula_key"], n): status
        for e in EVENTS
        for n, status in e["statuses"].items()
    }
    expected = [
        {
            "client_id": k["client_id"],
            "formula_key": k["formula_key"],
            "week": n,
            "week_start": ws.isoformat(),
            "expected_status": planted.get((k["client_id"], k["formula_key"], n), "green"),
        }
        for k in kpis
        for n, ws in enumerate(weeks, start=1)
    ]
    events = [
        {
            "client_id": e["client_id"],
            "formula_key": e["formula_key"],
            "weeks": [
                {"week": n, "week_start": weeks[n - 1].isoformat()} for n in sorted(e["statuses"])
            ],
            "top_drivers": e["top_drivers"],
            "driver_dimension": e["driver_dimension"],
            "description": e["description"],
        }
        for e in EVENTS
    ]
    return {
        "seed": SEED,
        "week_starts": [w.isoformat() for w in weeks],
        "false_alarm_rule": "a computed status other than green where expected_status is green",
        "expected_statuses": expected,  # one entry per client x KPI x week (120)
        "events": events,
    }


def build_source_loads(weeks: list[date], facts: list[dict]) -> list[dict]:
    """One row per client x week x report, with how many fact rows it produced."""
    counts = Counter(
        (f["client_id"], f["week_start"], METRIC_SOURCE[f["metric_key"]]) for f in facts
    )
    return [
        {
            "client_id": c,
            "week_start": ws.isoformat(),
            "source": s,
            "row_count": counts[(c, ws.isoformat(), s)],
        }
        for c, sources in CLIENT_SOURCES.items()
        for ws in weeks
        for s in sources
    ]


def generate(seed: int = SEED) -> dict:
    """Build every dataset in memory. Pure: same seed -> same output."""
    rng = random.Random(seed)
    weeks = week_starts()
    facts = []
    for n, ws in enumerate(weeks, start=1):
        facts += brightline_week(rng, n, ws)
        facts += northgate_week(rng, n, ws)
    kpis = build_kpi_definitions()
    return {
        "clients": build_clients(),
        "kpi_definitions": kpis,
        "source_facts": facts,
        "source_loads": build_source_loads(weeks, facts),
        "docs": build_docs(kpis),
        "ground_truth": build_ground_truth(weeks, kpis),
    }


def write(data: dict, out_dir: Path = OUT_DIR) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name in ("clients", "kpi_definitions", "docs", "ground_truth"):
        (out_dir / f"{name}.json").write_text(json.dumps(data[name], indent=2) + "\n")
    with open(out_dir / "source_facts.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["client_id", "week_start", "metric_key", "value", "detail_json"])
        for row in data["source_facts"]:
            writer.writerow(
                [
                    row["client_id"],
                    row["week_start"],
                    row["metric_key"],
                    f"{row['value']:.2f}",
                    json.dumps(row["detail"], sort_keys=True),
                ]
            )
    with open(out_dir / "source_loads.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["client_id", "week_start", "source", "row_count"])
        for row in data["source_loads"]:
            writer.writerow([row["client_id"], row["week_start"], row["source"], row["row_count"]])


def main() -> None:
    data = generate()
    write(data)
    print(f"wrote {len(data['source_facts'])} facts and {len(data['docs'])} docs to {OUT_DIR}")


if __name__ == "__main__":
    main()
