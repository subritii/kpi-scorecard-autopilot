"""Templates built only from computed numbers (pure). They must always pass the
faithfulness check, since they are the fallback when the LLM's text doesn't."""

from dataclasses import replace
from decimal import Decimal as D

from test_faithfulness import AR, CTX, GM, OWNER_REPLY, REVENUE

from app import packet
from app.faithfulness import check, render_owner_quote

OWNERS = {
    AR.kpi_id: ("Avery Chen", "avery.chen@brightline.example"),
    GM.kpi_id: ("Lena Brooks", "lena.brooks@brightline.example"),
    REVENUE.kpi_id: ("Sam Keller", "sam.keller@brightline.example"),
}


# --- formatting ------------------------------------------------------------------


def test_display_formats():
    assert packet.display("ar_over_60", D("114983.125")) == "$114,983.13"  # half-up
    assert packet.display("ar_over_60", D("57749.06"), signed=True) == "+$57,749.06"
    assert packet.display("ar_over_60", D("-629"), signed=True) == "-$629.00"
    assert packet.display("gross_margin_pct", D("47.0612")) == "47.1%"
    assert packet.display("gross_margin_pct", D("-1.2388"), signed=True) == "-1.2 points"
    assert packet.display("inventory_days", D("55.06")) == "55.1 days"
    assert packet.display("ar_over_60", None) == "no data"


def test_week_label():
    assert packet.week_label(CTX.week_start) == "Aug 3, 2026"


# --- observation template ---------------------------------------------------------


def test_observation_template_states_the_move_and_movers():
    text = packet.observation_template(AR, CTX.week_start)
    assert text.startswith("For the week of Aug 3, 2026, AR Over 60 Days rose to $114,983.12")
    assert "from $57,234.06" in text and "+$57,749.06" in text
    assert "Ambercrest Logistics (+$32,743.28, 54.1% of the movement)" in text
    assert "because" not in text and "due to" not in text


def test_observation_template_passes_the_checker_for_every_kpi():
    for kpi in CTX.kpis:
        text = packet.observation_template(kpi, CTX.week_start)
        assert check(text, CTX).passed, (kpi.name, text, check(text, CTX).problems)


def test_observation_template_without_data_or_history():
    no_data = replace(AR, value=None, prior_value=None, net_change=None, contributors=())
    text = packet.observation_template(no_data, CTX.week_start)
    assert "no data" in text and check(text, replace(CTX, kpis=(no_data,))).passed
    first_week = replace(AR, prior_value=None, net_change=None)
    text = packet.observation_template(first_week, CTX.week_start)
    assert "from" not in text and check(text, replace(CTX, kpis=(first_week,))).passed


def test_flat_kpi_says_unchanged():
    flat = replace(REVENUE, prior_value=REVENUE.value, net_change=D("0"), contributors=())
    text = packet.observation_template(flat, CTX.week_start)
    assert "was unchanged at $131,000.50" in text
    assert check(text, replace(CTX, kpis=(flat,))).passed


# --- owner message ------------------------------------------------------------------


def test_owner_message_has_the_observation_and_the_four_questions():
    msg = packet.owner_message("Avery Chen", "AR Over 60 Days rose.", past_explanation=None)
    assert msg.startswith("Hi Avery,")
    assert "AR Over 60 Days rose." in msg
    for q in ("What changed?", "Why did it change?", "Who owns the fix?", "What's being done?"):
        assert q in msg
    assert "last year" not in msg


def test_owner_message_quotes_last_years_explanation_verbatim():
    msg = packet.owner_message(
        "Avery Chen", "AR rose.", past_explanation=("Aug 4, 2025", "Two customers paid late.")
    )
    assert "> Two customers paid late." in msg
    assert "week of Aug 4, 2025" in msg


# --- packet ---------------------------------------------------------------------------


def test_packet_inserts_replies_verbatim_and_passes_the_checker():
    md = packet.assemble(CTX, OWNERS, replies={AR.kpi_id: OWNER_REPLY})
    assert render_owner_quote(OWNER_REPLY) in md
    result = check(md, CTX)
    assert result.passed, result.problems


def test_packet_marks_unanswered_red_kpis():
    md = packet.assemble(CTX, OWNERS, replies={})
    # Weekly Revenue and AR Over 60 Days are red; neither has a reply.
    assert "Replies missing for: Weekly Revenue and AR Over 60 Days." in md
    assert md.count("No reply yet") == 2
    assert "avery.chen@brightline.example" in md
    assert check(md, CTX).passed


def test_packet_has_a_row_per_kpi_and_a_section_per_red_kpi():
    md = packet.assemble(CTX, OWNERS, replies={AR.kpi_id: OWNER_REPLY})
    for kpi in CTX.kpis:
        assert f"| {kpi.name} |" in md
    assert "## AR Over 60 Days (red)" in md and "## Weekly Revenue (red)" in md
    assert "## Gross Margin %" not in md  # green: table row only
    assert md.startswith("# KPI packet: Brightline Services, week of Aug 3, 2026")
