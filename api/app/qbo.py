"""Parse QuickBooks Online's AgedReceivables report (JSON). Pure: no I/O.

The report is a table: one column per aging bucket ("Current", "1 - 30",
"31 - 60", "61 - 90", "91 and over", "Total"), one Data row per customer.
Customers with sub-customers (jobs, locations) come as a Section: a Header
with the parent's name, nested Rows, and a Summary with the section total.
The last row is the grand total (group "GrandTotal").

We keep the 61-90 and 91+ buckets (AR over 60), per row, attributed to the
top-level customer (so drivers group by the actual customer). Amounts are
Decimal from QuickBooks' strings; anything else is refused. The parsed totals
must equal each section's and the report's own totals, so a report that was
misread is never loaded.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

REPORT_NAME = "AgedReceivables"
# Column titles for the default aging settings (30-day periods). Other
# settings rename the columns, which is refused rather than guessed at.
COL_61_90 = "61 - 90"
COL_91_PLUS = "91 and over"


class ReportError(ValueError):
    """The report isn't an AgedReceivables report we can read safely (HTTP 422)."""


@dataclass(frozen=True)
class AgingRow:
    customer: str  # the top-level customer
    job: str | None  # sub-customer under it, if any
    qbo_id: str | None
    ar_61_90: Decimal
    ar_91_plus: Decimal


@dataclass(frozen=True)
class AgingReport:
    as_of: date
    rows: list[AgingRow]
    total_61_90: Decimal
    total_91_plus: Decimal


def _amount(cell: dict, where: str) -> Decimal:
    value = cell.get("value", "")
    if value == "":
        return Decimal(0)  # QuickBooks leaves zero cells empty
    if not isinstance(value, str):
        # A JSON number may already have been through a float: refuse it.
        raise ReportError(f"{where}: amount {value!r} is not a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as e:
        raise ReportError(f"{where}: amount {value!r} is not a number") from e
    if not amount.is_finite():
        raise ReportError(f"{where}: amount {value!r} is not a number")
    return amount


def _as_of(header: dict) -> date:
    options = {o.get("Name"): o.get("Value") for o in header.get("Option", [])}
    raw = options.get("report_date") or header.get("EndPeriod")
    try:
        return date.fromisoformat(raw)
    except (TypeError, ValueError) as e:
        raise ReportError(f"report has no readable date (report_date / EndPeriod): {raw!r}") from e


def parse_aged_receivables(report: dict) -> AgingReport:
    if not isinstance(report, dict) or not isinstance(report.get("Header"), dict):
        raise ReportError("not a QuickBooks report object")
    header = report["Header"]
    if header.get("ReportName") != REPORT_NAME:
        raise ReportError(f"expected the {REPORT_NAME} report, got {header.get('ReportName')!r}")
    as_of = _as_of(header)

    titles = [c.get("ColTitle", "") for c in report.get("Columns", {}).get("Column", [])]
    missing = [t for t in (COL_61_90, COL_91_PLUS) if t not in titles]
    if missing:
        raise ReportError(f"missing column(s) {missing}; the report has {titles}")
    i61, i91 = titles.index(COL_61_90), titles.index(COL_91_PLUS)

    rows: list[AgingRow] = []
    grand: tuple[Decimal, Decimal] | None = None

    def buckets(col_data: list, where: str) -> tuple[Decimal, Decimal]:
        if len(col_data) != len(titles):
            raise ReportError(f"{where}: {len(col_data)} cells, expected {len(titles)}")
        return _amount(col_data[i61], where), _amount(col_data[i91], where)

    def walk(items: list, top: str | None) -> tuple[Decimal, Decimal]:
        """Collect Data rows under `top` (None at the top level); return their sums."""
        nonlocal grand
        s61 = s91 = Decimal(0)
        for item in items:
            if "ColData" in item:  # a customer, or a sub-customer inside a section
                name = item["ColData"][0].get("value", "")
                a61, a91 = buckets(item["ColData"], name)
                rows.append(
                    AgingRow(
                        customer=top or name,
                        job=name if top else None,
                        qbo_id=item["ColData"][0].get("id"),
                        ar_61_90=a61,
                        ar_91_plus=a91,
                    )
                )
                s61, s91 = s61 + a61, s91 + a91
            elif item.get("group") == "GrandTotal":
                grand = buckets(item["Summary"]["ColData"], "grand total")
            elif "Header" in item:  # a parent customer with sub-customers
                name = item["Header"]["ColData"][0].get("value", "")
                n61, n91 = walk(item.get("Rows", {}).get("Row", []), top or name)
                if "Summary" in item:
                    t61, t91 = buckets(item["Summary"]["ColData"], f"total of {name}")
                    if (t61, t91) != (n61, n91):
                        raise ReportError(
                            f"{name}: rows add up to {n61} / {n91} but the section total "
                            f"says {t61} / {t91}"
                        )
                s61, s91 = s61 + n61, s91 + n91
        return s61, s91

    try:
        total_61, total_91 = walk(report.get("Rows", {}).get("Row", []), None)
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise ReportError(f"unexpected report structure: {e!r}") from e

    if rows and grand is None:
        raise ReportError("report has rows but no grand total to check them against")
    if grand is not None and grand != (total_61, total_91):
        raise ReportError(
            f"rows add up to {total_61} (61-90) / {total_91} (91+), but the grand total "
            f"says {grand[0]} / {grand[1]}"
        )
    if grand is not None:
        # Equal to our sums (checked above); the report's own values keep its
        # precision, e.g. "0.00" rather than a bare "0".
        total_61, total_91 = grand
    return AgingReport(as_of=as_of, rows=rows, total_61_90=total_61, total_91_plus=total_91)
