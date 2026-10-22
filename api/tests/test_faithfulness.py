"""Tests for app.faithfulness: the checker for AI-written packet text.

The run context below is written by hand (a Brightline week like week 5):
AR over 60 rose and is red, gross margin slipped, revenue is 131,000.50 (red).
"""

from datetime import date
from decimal import Decimal as D

import pytest

from app.faithfulness import (
    Contributor,
    Denominator,
    KpiContext,
    OwnerResponse,
    RunContext,
    check,
    render_owner_quote,
)

AR = KpiContext(
    kpi_id=4,
    formula_key="ar_over_60",
    name="AR Over 60 Days",
    direction="lower",
    value=D("114983.12"),
    prior_value=D("57234.06"),
    target=D("90000"),
    net_change=D("57749.06"),
    contributors=(
        Contributor(
            "Ambercrest Logistics", D("7386.12"), D("40129.40"), D("32743.28"), D("0.5408")
        ),
        Contributor("Larkspur Clinics", D("7251.11"), D("33407.82"), D("26156.71"), D("0.4320")),
        Contributor(
            "Orchard Vale Architects", D("7000.00"), D("6371.00"), D("-629.00"), D("0.0104")
        ),
    ),
    status="red",
    red_streak=1,
)
GM = KpiContext(
    kpi_id=2,
    formula_key="gross_margin_pct",
    name="Gross Margin %",
    direction="higher",
    value=D("47.0612345"),
    prior_value=D("48.30"),
    target=D("42"),
    net_change=D("-1.2387655"),
    contributors=(
        Contributor("Subcontractor fees", D("20000"), D("20600"), D("600"), D("0.6")),
        Contributor("Direct labor", D("57000"), D("56600"), D("-400"), D("0.4")),
    ),
    denominator=Denominator("revenue", D("173000"), D("172000"), D("-1000")),
    status="green",
)
REVENUE = KpiContext(
    kpi_id=1,
    formula_key="weekly_revenue",
    name="Weekly Revenue",
    direction="higher",
    value=D("131000.50"),
    prior_value=D("130000.00"),
    target=D("155000"),
    net_change=D("1000.50"),
    contributors=(
        Contributor("Quillfield Dental Group", D("20000"), D("21000.50"), D("1000.50"), D("1")),
    ),
    status="red",
    red_streak=5,
)
OWNER_REPLY = OwnerResponse(
    kpi_id=4,
    what_changed="Two customers paid late; about $59,000 is now overdue.",
    why="Ambercrest moved to a new AP system, and Larkspur disputed 3 invoices because of "
    "a billing error.",
    owner_of_fix="Avery Chen",
    actions="Calls with both AP teams; payment expected by Aug 14.",
)
CTX = RunContext(
    week_start=date(2026, 8, 3),
    client_name="Brightline Services",
    people=("Avery Chen", "Lena Brooks", "Sam Keller"),
    kpis=(REVENUE, GM, AR),
    owner_responses=(OWNER_REPLY,),
)

AI_TEXT = (
    "For the week of Aug 3, 2026, AR Over 60 Days rose to $115.0K, up $57.7K from "
    "$57.2K and above the $90K target. The largest movers were Ambercrest Logistics "
    "(+$32.7K, 54.1% of the movement) and Larkspur Clinics (+$26,156.71, 43%). "
    "Gross Margin % slipped to 47.1% from 48.3%, down 1.2 points; Subcontractor fees rose "
    "$600 while Direct labor fell $400. Weekly Revenue was $131,000.50."
)
PACKET = AI_TEXT + "\n\n" + render_owner_quote(OWNER_REPLY)


def problems(text, ctx=CTX, kind=None):
    found = check(text, ctx).problems
    return [p for p in found if kind is None or p.type == kind]


# --- passing -------------------------------------------------------------------


def test_faithful_packet_passes():
    result = check(PACKET, CTX)
    assert result.problems == []
    assert result.passed is True


def test_owner_quoted_numbers_names_and_causes_do_not_fail():
    # The quote has a number not in the data ($59,000), a count (3), a date
    # (Aug 14), a name (Ambercrest, alone) and "because". All exempt: it's the
    # owner's verbatim reply, not AI text.
    quote = render_owner_quote(OWNER_REPLY)
    assert "$59,000" in quote and "because" in quote
    assert check(quote, CTX).passed


def test_owner_quote_format():
    assert render_owner_quote(OWNER_REPLY) == (
        "<!-- owner-quote kpi_id=4 -->\n"
        "> **What changed:** Two customers paid late; about $59,000 is now overdue.\n"
        "> **Why:** Ambercrest moved to a new AP system, and Larkspur disputed 3 invoices "
        "because of a billing error.\n"
        "> **Who owns the fix:** Avery Chen\n"
        "> **What's being done:** Calls with both AP teams; payment expected by Aug 14.\n"
        "<!-- /owner-quote -->"
    )


def test_led_by_and_driven_by_are_not_causal():
    # They say which contributors moved the number, not why.
    assert check("AR Over 60 Days rose $57.7K, led by Ambercrest Logistics.", CTX).passed
    assert check("AR Over 60 Days rose $57.7K, driven by Larkspur Clinics.", CTX).passed


def test_kpi_names_with_digits_and_bucket_labels_are_not_numbers():
    text = "AR over 60 days rose; most of it sits in the 61-90 bucket, not 90+."
    assert check(text, CTX).passed


# --- owner quotes must be genuine ------------------------------------------------


def test_edited_owner_quote_fails():
    tampered = PACKET.replace("$59,000", "$5,900")
    [p] = problems(tampered, kind="owner_quote")
    assert "does not match" in p.reason


def test_unclosed_owner_quote_fails():
    text = AI_TEXT + "\n<!-- owner-quote kpi_id=4 -->\n> **Why:** made up"
    assert problems(text, kind="owner_quote")


def test_owner_quote_for_unknown_kpi_fails():
    fake = render_owner_quote(OWNER_REPLY).replace("kpi_id=4", "kpi_id=99")
    assert problems(fake, kind="owner_quote")


# --- numbers: formats and rounding ------------------------------------------------


def test_made_up_number_fails():
    [p] = problems("AR Over 60 Days rose to $120.0K.", kind="number")
    assert p.text == "$120.0K"
    assert "not in this run" in p.reason


# Weekly Revenue is 131,000.50. The text's own precision sets the tolerance:
# "$131K" means 130,500-131,500; "$131,000.50" means exactly that, to the cent.
@pytest.mark.parametrize(
    "written, ok",
    [
        ("$131K", True),
        ("$131.0K", True),
        ("131,000", True),
        ("$131,000.50", True),
        ("$131,000.5", True),
        ("131000.50", True),
        ("$0.13M", True),
        ("$132K", False),  # off by 999.50 at a precision of 1,000
        ("$131.1K", False),  # wrong at the stated precision
        ("$131,000.40", False),  # to-the-cent formats must be right to the cent
        ("$1.31M", False),  # ten times too big
        ("$13.1K", False),
    ],
)
def test_number_formats(written, ok):
    assert check(f"Weekly Revenue was {written}.", CTX).passed is ok


@pytest.mark.parametrize(
    "written, ok",
    [
        ("47.1%", True),  # 47.0612 rounds to 47.1
        ("47.06%", True),
        ("47%", True),
        ("47.0%", False),  # truncated, not rounded
        ("47.2%", False),
    ],
)
def test_percent_rounding_edge(written, ok):
    assert check(f"Gross Margin % was {written}.", CTX).passed is ok


@pytest.mark.parametrize(
    "written, ok",
    [
        ("$115.0K", True),
        ("$115K", True),
        ("$114,983", True),
        ("$114.9K", False),
        ("$114,983.10", False),
    ],
)
def test_money_rounding_edge(written, ok):
    assert check(f"AR Over 60 Days was {written}.", CTX).passed is ok


def test_shares_match_as_percentages():
    assert check("Ambercrest Logistics was 54% of the movement.", CTX).passed
    assert not check("Ambercrest Logistics was 64% of the movement.", CTX).passed


def test_wrong_sign_on_a_change_fails():
    [p] = problems("Ambercrest Logistics (-$32.7K) moved the most.", kind="direction")
    assert p.text == "-$32.7K"


# --- numbers: scoped to their KPI, and to tense ------------------------------------


def test_present_tense_prior_value_fails():
    # 130,000 is revenue's prior value; "was" claims it is this week's.
    [p] = problems("Weekly Revenue was $130K.", kind="number")
    assert p.text == "$130K"
    assert "prior value" in p.reason


@pytest.mark.parametrize(
    "text",
    [
        "Weekly Revenue was $131K, up from $130K.",
        "Weekly Revenue was $131K; last week it was $130K.",
        "Weekly Revenue was previously $130K.",
        "Gross Margin % is now 47.1%, from 48.3%.",
    ],
)
def test_prior_value_with_comparison_words_passes(text):
    assert check(text, CTX).passed


def test_number_from_another_kpi_fails():
    # $115.0K is AR's value; it doesn't belong in a revenue statement.
    [p] = problems("Weekly Revenue was $115.0K.", kind="number")
    assert "AR Over 60 Days" in p.reason


def test_contributor_numbers_belong_to_their_kpi():
    # Direct labor is a COGS account under Gross Margin %, so its $400 is fine there ...
    assert check("Direct labor fell $400.", CTX).passed
    # ... but Quillfield Dental Group (a revenue contributor) didn't move by $400.
    assert not check("Quillfield Dental Group fell $400.", CTX).passed


def test_section_heading_scopes_numbers_without_a_kpi_in_the_sentence():
    assert check("## AR Over 60 Days\n\nIt rose to $115.0K.", CTX).passed
    assert not check("## Weekly Revenue\n\nIt rose to $115.0K.", CTX).passed


def test_number_tied_to_no_kpi_fails():
    [p] = problems("Overall, $115.0K was the headline.", kind="number")
    assert "not tied to a KPI" in p.reason


def test_target_and_change_use_their_own_values():
    assert check("AR Over 60 Days is above its $90K target.", CTX).passed
    assert not check("AR Over 60 Days is above its $95K target.", CTX).passed
    assert check("AR Over 60 Days rose $57.7K.", CTX).passed
    assert not check(
        "AR Over 60 Days rose $115.0K.", CTX
    ).passed  # that's the level, not the change


# --- counts -------------------------------------------------------------------------


def test_contributor_count_must_be_real():
    assert check("AR Over 60 Days moved across 3 customers.", CTX).passed
    [p] = problems("AR Over 60 Days moved across 4 customers.", kind="number")
    assert p.text == "4"


def test_red_kpi_count_must_be_real():
    # Weekly Revenue and AR Over 60 Days are red: two.
    assert check("This week 2 KPIs are red.", CTX).passed
    assert not check("This week 4 KPIs are red.", CTX).passed


def test_consecutive_red_weeks_must_be_real():
    assert check("Weekly Revenue has been red for 5 weeks.", CTX).passed
    assert not check("Weekly Revenue has been red for 3 weeks.", CTX).passed
    assert check("AR Over 60 Days has been red for 1 week.", CTX).passed


# --- dates --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written, ok",
    [
        ("Aug 3, 2026", True),  # week start
        ("Aug 3", True),
        ("August 3, 2026", True),
        ("8/3/2026", True),
        ("2026-08-03", True),
        ("August 9", True),  # week end (Sunday)
        ("Jul 27, 2026", True),  # prior week start, for comparisons
        ("Jul 20, 2026", False),
        ("Aug 10, 2026", False),
        ("9/3/2026", False),
        ("2026-08-10", False),
    ],
)
def test_dates_must_match_the_run(written, ok):
    result = check(f"For the week of {written}, AR Over 60 Days rose.", CTX)
    assert result.passed is ok
    # A date is a date, right or wrong: never reported as stray numbers.
    assert not [p for p in result.problems if p.type == "number"]


def test_iso_week_numbers_are_not_used():
    # Packets name weeks by date ("week of Aug 3, 2026"), so "Week 32" is flagged
    # even though it is this run's ISO week.
    [p] = problems("In Week 32, AR Over 60 Days rose.", kind="week")
    assert p.text == "Week 32"
    assert "week of Aug 3, 2026" in p.reason


# --- names ------------------------------------------------------------------------


def test_invented_name_fails():
    found = problems("Bluewater Freight added to AR Over 60 Days.", kind="name")
    assert [p.text for p in found] == ["Bluewater Freight"]


def test_another_clients_customer_fails():
    # Valemont Hardware is a Northgate customer; it isn't in this run's drivers.
    assert [p.text for p in problems("AR rose at Valemont Hardware.", kind="name")] == [
        "Valemont Hardware"
    ]


def test_single_capitalized_word_mid_sentence_fails():
    assert [p.text for p in problems("AR Over 60 Days rose at Northwind.", kind="name")] == [
        "Northwind"
    ]


def test_known_names_pass():
    text = (
        "Brightline Services: Avery Chen owns AR Over 60 Days. In August, "
        "Quillfield Dental Group added $1,000.50 to Weekly Revenue."
    )
    assert check(text, CTX).passed


# --- direction ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "AR Over 60 Days fell to $115.0K.",  # it rose
        "Gross Margin % rose to 47.1%.",  # it fell
        "Direct labor rose $400.",  # contributor fell by 400
        "AR Over 60 Days improved this week.",  # lower is better, and it rose
        "Weekly Revenue declined slightly.",  # it rose
    ],
)
def test_wrong_direction_fails(text):
    [p] = problems(text, kind="direction")
    assert p.text in text and p.reason


def test_direction_verb_attaches_to_the_nearest_item_before_it():
    # "fell" belongs to Direct labor (which fell), not to Gross Margin % before it.
    assert check("Gross Margin % slipped; Direct labor fell $400.", CTX).passed


# --- causal language ------------------------------------------------------------------


@pytest.mark.parametrize("phrase", ["because", "due to", "caused by", "as a result of", "Due to"])
def test_causal_language_fails(phrase):
    text = f"AR Over 60 Days rose to $115.0K {phrase} late payments."
    [p] = problems(text, kind="causal")
    assert p.text == phrase
    assert "owner" in p.reason


# --- result shape ------------------------------------------------------------------------


def test_every_problem_is_reported_with_exact_text_and_reason():
    text = (
        "AR Over 60 Days fell to $120.0K because Bluewater Freight paid late in Week 31. "
        + render_owner_quote(OWNER_REPLY)
    )
    result = check(text, CTX)
    assert result.passed is False
    assert {p.type for p in result.problems} == {"number", "direction", "causal", "name", "week"}
    for p in result.problems:
        assert p.text and p.text in text
        assert p.reason
