"""Parsing QuickBooks Online's AgedReceivables report (pure; never a live call).

The primary fixture is a real sandbox response; the hand-written sample only
covers edge cases the real one doesn't (deeper nesting, sub-cent sums).
"""

import copy
import json
from datetime import date
from decimal import Decimal as D

import pytest

from app import qbo
from app.config import REPO_ROOT

SAMPLES = REPO_ROOT / "data" / "samples"
# The saved file is n8n's output: a list holding the one report object.
[REAL] = json.loads((SAMPLES / "qbo_aged_receivables_real.json").read_text())
HANDWRITTEN = json.loads((SAMPLES / "qbo_aged_receivables_handwritten.json").read_text())


def real() -> dict:
    return copy.deepcopy(REAL)


# --- the real sandbox report -------------------------------------------------------


def test_real_report_totals_match_its_grand_total():
    report = qbo.parse_aged_receivables(real())
    assert report.as_of == date(2026, 10, 22)
    assert (report.total_61_90, report.total_91_plus) == (D("156.00"), D("0.00"))


def test_real_report_rows_by_customer():
    report = qbo.parse_aged_receivables(real())
    assert len(report.rows) == 17  # every Data row, including sub-customers
    red_rock = next(r for r in report.rows if r.customer == "Red Rock Diner")
    assert (red_rock.ar_61_90, red_rock.ar_91_plus, red_rock.qbo_id) == (D("156.00"), D("0"), "20")
    assert all(isinstance(r.ar_61_90, D) and isinstance(r.ar_91_plus, D) for r in report.rows)


def test_sub_customers_belong_to_their_top_level_customer():
    report = qbo.parse_aged_receivables(real())
    freeman = [r for r in report.rows if r.customer == "Freeman Sporting Goods"]
    assert sorted(r.job for r in freeman) == ["0969 Ocean View Road", "55 Twin Lane"]
    assert next(r for r in report.rows if r.job == "Barnett Design").customer == "Shara Barnett"
    assert next(r for r in report.rows if r.customer == "Amy's Bird Sanctuary").job is None


# --- edge cases (hand-written sample) -----------------------------------------------------


def test_deeper_nesting_and_empty_cells():
    report = qbo.parse_aged_receivables(HANDWRITTEN)
    by_job = {r.job: r for r in report.rows}
    # Two levels down, still attributed to the top-level customer.
    site = by_job["Region B Site 1"]
    assert site.customer == "Fictional Parent Co"
    assert (site.ar_61_90, site.ar_91_plus) == (D("25.50"), D("1000.00"))
    assert by_job["Parent Job A"].ar_91_plus == D("0")  # empty cell = 0


def test_sums_are_exact_decimals():
    # 0.10 + 125.50 = 125.60 and 0.20 + 1000.00 = 1000.20 exactly (no float drift).
    report = qbo.parse_aged_receivables(HANDWRITTEN)
    assert (report.total_61_90, report.total_91_plus) == (D("125.60"), D("1000.20"))


# --- refusals: a report that doesn't check out is never half-loaded ---------------------------


def test_grand_total_mismatch_is_refused():
    report = real()
    grand = report["Rows"]["Row"][-1]
    assert grand["group"] == "GrandTotal"
    grand["Summary"]["ColData"][4]["value"] = "999.00"  # 61 - 90
    with pytest.raises(qbo.ReportError, match="grand total"):
        qbo.parse_aged_receivables(report)


def test_section_subtotal_mismatch_is_refused():
    report = copy.deepcopy(HANDWRITTEN)
    section = report["Rows"]["Row"][1]
    section["Summary"]["ColData"][4]["value"] = "1.00"
    with pytest.raises(qbo.ReportError, match="Fictional Parent Co"):
        qbo.parse_aged_receivables(report)


def test_wrong_report_is_refused():
    report = real()
    report["Header"]["ReportName"] = "ProfitAndLoss"
    with pytest.raises(qbo.ReportError, match="AgedReceivables"):
        qbo.parse_aged_receivables(report)


def test_missing_bucket_column_is_refused_and_lists_what_was_found():
    report = real()
    report["Columns"]["Column"][5]["ColTitle"] = "91+"  # e.g. different aging settings
    with pytest.raises(qbo.ReportError, match=r"91 and over.*91\+"):
        qbo.parse_aged_receivables(report)


@pytest.mark.parametrize("bad", [156.0, 156, "abc", "NaN", "Infinity"])
def test_amount_that_is_not_a_decimal_string_is_refused(bad):
    report = real()
    red_rock = next(
        r
        for r in report["Rows"]["Row"]
        if r.get("ColData", [{}])[0].get("value") == "Red Rock Diner"
    )
    red_rock["ColData"][4]["value"] = bad
    with pytest.raises(qbo.ReportError, match="Red Rock Diner"):
        qbo.parse_aged_receivables(report)


def test_missing_grand_total_is_refused():
    report = real()
    report["Rows"]["Row"].pop()
    with pytest.raises(qbo.ReportError, match="grand total"):
        qbo.parse_aged_receivables(report)


def test_report_with_no_data_is_empty_not_an_error():
    report = real()
    report["Rows"] = {}
    report["Header"]["Option"] = [
        {"Name": "report_date", "Value": "2026-10-22"},
        {"Name": "NoReportData", "Value": "true"},
    ]
    parsed = qbo.parse_aged_receivables(report)
    assert parsed.rows == [] and parsed.total_61_90 == D("0")


@pytest.mark.parametrize("not_a_report", [[], "text", None, {"Header": {}}])
def test_malformed_input_is_refused(not_a_report):
    with pytest.raises(qbo.ReportError):
        qbo.parse_aged_receivables(not_a_report)


def test_parser_is_pure():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(qbo))
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }
    assert imported <= {"dataclasses", "datetime", "decimal"}, imported
