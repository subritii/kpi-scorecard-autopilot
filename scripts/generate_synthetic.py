"""Generate synthetic data: 2 fictional clients x 12 weeks, with planted events.

Writes to data/seed/:
  clients.json          client records
  kpi_definitions.json  KPIs, owners, targets
  source_facts.csv      raw weekly numbers, one row per metric per dimension
  docs.json             KPI definitions, notes, and last year's explanations (for RAG)
  ground_truth.json     the planted events, used by the eval

A fixed seed makes every run produce identical output. All companies and
people are invented; emails use the reserved .example domain.

Noise ranges and planted amounts are sized so that normal weeks always stay
green and planted weeks always land in the intended status, whatever the
random draws are (tests/test_synthetic_data.py checks this).
"""

import csv
import json
import random
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
# Planted: week 8, the staffing partner raises rates -> subcontractor fees double.
PRICE_INCREASE_WEEK = 8
PRICE_INCREASE_ACCOUNT = "Subcontractor fees"
PRICE_INCREASE_RATE = 0.24
# Planted: weeks 10-11, seasonal dip. Multiplier on normal bookings
# (0.846 lands in the yellow band, 0.70 is clearly red).
BOOKINGS_DIP = {10: 0.846, 11: 0.70}

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
# Planted: week 6, an overstock purchase of plumbing fixtures.
OVERSTOCK_WEEK = 6
OVERSTOCK_CATEGORY = "Plumbing fixtures"
OVERSTOCK_AMOUNT = 420_000


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

    for customer in BRIGHTLINE_CUSTOMERS:
        b61 = 4_500 * jitter(rng, 0.15)
        b90 = 2_400 * jitter(rng, 0.15)
        extra61, extra90 = LATE_PAYERS.get(customer, {}).get(week, (0, 0))
        rows += ar_rows(c, ws, customer, b61 + extra61, b90 + extra90)

    dip = BOOKINGS_DIP.get(week)
    for line, amount in BRIGHTLINE_BOOKINGS.items():
        value = amount * dip * jitter(rng, 0.01) if dip else amount * jitter(rng, 0.05)
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

    rows.append(
        fact(c, ws, "cogs", total_revenue * 0.655 * jitter(rng, 0.02), account="Product cost")
    )
    rows.append(fact(c, ws, "cogs", total_revenue * 0.03 * jitter(rng, 0.05), account="Freight-in"))

    for account, balance in NORTHGATE_CASH.items():
        rows.append(fact(c, ws, "cash_ending", balance * jitter(rng, 0.04), account=account))

    for customer in NORTHGATE_CUSTOMERS:
        rows += ar_rows(c, ws, customer, 8_000 * jitter(rng, 0.15), 4_000 * jitter(rng, 0.15))

    for category, balance in NORTHGATE_INVENTORY.items():
        value = balance * jitter(rng, 0.03)
        if week == OVERSTOCK_WEEK and category == OVERSTOCK_CATEGORY:
            value += OVERSTOCK_AMOUNT
        rows.append(fact(c, ws, "inventory_balance", value, category=category))

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
            "What changed: AR over 60 days rose well above target. "
            "Why: Ambercrest Logistics and Mosswood Hospitality held payments while moving to "
            "a new AP system. Owner of fix: Avery Chen. Actions: weekly calls with both AP "
            "teams; both balances cleared within three weeks.",
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
            "Why: late-summer slowdown; prospects pushed signing decisions until their new "
            "budget cycle. Owner of fix: Marcus Hale. Actions: front-loaded proposals in "
            "August; pipeline recovered by October.",
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
            "What changed: inventory days spiked above target for one week. "
            "Why: purchasing placed an early bulk order of plumbing fixtures ahead of an "
            "announced supplier price increase. Owner of fix: Tom Lindqvist. Actions: sold "
            "through the excess over the next month; added a second approval for large POs.",
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
    return docs


def build_ground_truth(weeks: list[date]) -> dict:
    def ws(n: int) -> str:
        return weeks[n - 1].isoformat()

    def event(client_id, formula_key, statuses, top_drivers, driver_dimension, description):
        return {
            "client_id": client_id,
            "formula_key": formula_key,
            "weeks": [
                {"week": n, "week_start": ws(n), "expected_status": s} for n, s in statuses.items()
            ],
            "top_drivers": top_drivers,
            "driver_dimension": driver_dimension,
            "description": description,
        }

    return {
        "seed": SEED,
        "week_starts": [w.isoformat() for w in weeks],
        "default_status": "green",  # every client-week-KPI not listed below
        "events": [
            event(
                "brightline",
                "ar_over_60",
                {5: "red", 6: "red", 7: "red"},
                sorted(LATE_PAYERS),
                "customer",
                "Two customers pay late; their balances age from 61-90 into 90+.",
            ),
            event(
                "brightline",
                "gross_margin_pct",
                {PRICE_INCREASE_WEEK: "red"},
                [PRICE_INCREASE_ACCOUNT],
                "account",
                "Supplier price increase doubles subcontractor fees for one week.",
            ),
            event(
                "brightline",
                "new_bookings",
                {10: "yellow", 11: "red"},
                None,
                None,
                "Seasonal dip in new bookings.",
            ),
            event(
                "northgate",
                "inventory_days",
                {OVERSTOCK_WEEK: "red"},
                [OVERSTOCK_CATEGORY],
                "category",
                "Overstock purchase of plumbing fixtures.",
            ),
        ],
    }


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
        "docs": build_docs(kpis),
        "ground_truth": build_ground_truth(weeks),
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


def main() -> None:
    data = generate()
    write(data)
    print(f"wrote {len(data['source_facts'])} facts and {len(data['docs'])} docs to {OUT_DIR}")


if __name__ == "__main__":
    main()
