"""Owner-message and packet templates. Pure: built only from computed numbers.

observation_template() is the fallback owner observation when the LLM's text
fails the faithfulness check; assemble() builds the whole packet. Both must
always pass faithfulness.check() (tests/test_packet.py).

assemble_html() builds the same packet as HTML for the approval page and the
leadership email, directly from the data (no Markdown library), escaping every
value with html.escape. Inline styles only; no scripts, links or images.
"""

import html
import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app import kpis
from app.faithfulness import Contributor, KpiContext, OwnerResponse, RunContext, render_owner_quote

MOVERS_LISTED = 3  # top contributors named per KPI
QUESTIONS = ("What changed?", "Why did it change?", "Who owns the fix?", "What's being done?")


def week_label(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"  # "Aug 3, 2026"


def display(formula_key: str, value: Decimal | None, *, signed: bool = False) -> str:
    """Display a value the way packets do: % and days to 1 place, money to the cent.

    Rounding is half-up (kpis.round_for_display). signed=True is for changes:
    "+$57,749.06", "-1.2 points". Contributor amounts use formula_key "money".
    """
    if value is None:
        return "no data"
    rounded = kpis.round_for_display(formula_key, value)
    sign = "+" if signed and rounded > 0 else "-" if rounded < 0 else ""
    magnitude = abs(rounded)
    if formula_key == "gross_margin_pct":
        return f"{sign}{magnitude:.1f}" + (" points" if signed else "%")
    if formula_key == "inventory_days":
        return f"{sign}{magnitude:.1f} days"
    return f"{sign}${magnitude:,.2f}"


def share_label(share: Decimal) -> str:
    return f"{(share * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def movers(k: KpiContext) -> list[Contributor]:
    """Top contributors that actually moved (contributors are already ranked)."""
    return [c for c in k.contributors if c.change != 0][:MOVERS_LISTED]


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def observation_template(k: KpiContext, week_start: date) -> str:
    """What moved and which contributors moved it, from computed numbers only."""
    week = week_label(week_start)
    if k.value is None:
        return (
            f"For the week of {week}, {k.name} has no data: a source report for it was not loaded."
        )
    value, target = display(k.formula_key, k.value), display(k.formula_key, k.target)
    if k.prior_value is None or k.net_change is None:
        text = f"For the week of {week}, {k.name} was {value}, against a target of {target}."
    elif k.net_change == 0:
        text = (
            f"For the week of {week}, {k.name} was unchanged at {value}, "
            f"against a target of {target}."
        )
    else:
        verb = "rose" if k.net_change > 0 else "fell"
        text = (
            f"For the week of {week}, {k.name} {verb} to {value} from "
            f"{display(k.formula_key, k.prior_value)}, a change of "
            f"{display(k.formula_key, k.net_change, signed=True)}, against a target of {target}."
        )
    top = movers(k)
    if top:
        parts = [
            f"{c.name} ({display('money', c.change, signed=True)}"
            + (
                f", {share_label(c.share_of_movement)} of the movement"
                if c.share_of_movement
                else ""
            )
            + ")"
            for c in top
        ]
        lead = "The largest mover was" if len(parts) == 1 else "The largest movers were"
        text += f" {lead} {_join(parts)}."
    return text


def owner_message(
    owner_name: str,
    observation: str,
    past_explanation: tuple[str, str] | None,
    reply_url: str | None = None,
) -> str:
    """The message sent to a KPI owner. Only `observation` may be LLM-written.

    past_explanation is (week label, text) of last year's similar move, quoted
    verbatim for reference; it never goes through the LLM.
    """
    lines = [f"Hi {owner_name.split()[0]},", "", observation, "", "Please reply with:"]
    lines += [f"{i}. {q}" for i, q in enumerate(QUESTIONS, start=1)]
    if reply_url:
        lines += ["", "Reply using this form (the link works once):", reply_url]
    if past_explanation:
        week, text = past_explanation
        lines += ["", f"For reference, last year's explanation of a similar move (week of {week}):"]
        lines += [f"> {line}" for line in text.splitlines()]
    return "\n".join(lines)


def assemble(
    ctx: RunContext,
    owners: dict[int, tuple[str, str]],
    replies: dict[int, OwnerResponse],
) -> str:
    """The one-page packet: scorecard table, then a section per red KPI with the
    owner's reply quoted verbatim, or a clear "No reply yet" marker.

    owners maps kpi_id -> (owner name, owner email); replies maps kpi_id -> the
    latest reply.
    """
    red = [k for k in ctx.kpis if k.status == "red"]
    unanswered = [k for k in red if k.kpi_id not in replies]

    lines = [f"# KPI packet: {ctx.client_name}, week of {week_label(ctx.week_start)}", ""]
    if unanswered:
        lines += [f"**Replies missing for: {_join([k.name for k in unanswered])}.**", ""]

    lines += [
        "| KPI | This week | Last week | Change | Target | Status |",
        "|---|---|---|---|---|---|",
    ]
    for k in ctx.kpis:
        prior = (
            f"from {display(k.formula_key, k.prior_value)}" if k.prior_value is not None else "-"
        )
        change = (
            display(k.formula_key, k.net_change, signed=True) if k.net_change is not None else "-"
        )
        lines.append(
            f"| {k.name} | {display(k.formula_key, k.value)} | {prior} | {change} | "
            f"{display(k.formula_key, k.target)} target | {k.status} |"
        )

    for k in red:
        owner_name, owner_email = owners[k.kpi_id]
        lines += ["", f"## {k.name} (red)", "", observation_template(k, ctx.week_start), ""]
        if k.kpi_id in replies:
            lines += [f"**Reply from {owner_name}:**", "", render_owner_quote(replies[k.kpi_id])]
        else:
            lines.append(
                f"> **No reply yet** from {owner_name} ({owner_email}): this KPI is unanswered."
            )
    return "\n".join(lines) + "\n"


# --- HTML version ---------------------------------------------------------------------

# (background, text) per status, as inline styles: the only styling allowed.
STATUS_COLORS = {
    "green": "background:#d8f3dc;color:#1b4332",
    "yellow": "background:#fff3bf;color:#5c4400",
    "red": "background:#ffd6d6;color:#7a0000",
    "no_data": "background:#e9ecef;color:#495057",
}
_CELL = "padding:6px 10px;border:1px solid #dee2e6;text-align:left"


def _text(value: str) -> str:
    """Escape text for HTML; keep its line breaks."""
    return html.escape(value).replace("\n", "<br>")


def assemble_html(
    ctx: RunContext,
    owners: dict[int, tuple[str, str]],
    replies: dict[int, OwnerResponse],
) -> str:
    """The packet as an HTML fragment: the same content as assemble(), built from data.

    Every value (names, numbers, owner answers) goes through html.escape, so
    text like "<script>" in an owner's reply is shown, never run.
    """
    red = [k for k in ctx.kpis if k.status == "red"]
    unanswered = [k for k in red if k.kpi_id not in replies]
    out = [
        '<div style="font-family:system-ui,sans-serif;color:#1b1b1b;line-height:1.5">',
        f"<h1>KPI packet: {_text(ctx.client_name)}, "
        f"week of {_text(week_label(ctx.week_start))}</h1>",
    ]
    if unanswered:
        names = _text(_join([k.name for k in unanswered]))
        out.append(f"<p><strong>Replies missing for: {names}.</strong></p>")

    out.append('<table style="border-collapse:collapse">')
    headers = ("KPI", "This week", "Last week", "Change", "Target", "Status")
    out.append("<tr>" + "".join(f'<th style="{_CELL}">{h}</th>' for h in headers) + "</tr>")
    for k in ctx.kpis:
        prior = display(k.formula_key, k.prior_value) if k.prior_value is not None else "-"
        change = (
            display(k.formula_key, k.net_change, signed=True) if k.net_change is not None else "-"
        )
        cells = [
            k.name,
            display(k.formula_key, k.value),
            prior,
            change,
            display(k.formula_key, k.target),
        ]
        row = "".join(f'<td style="{_CELL}">{_text(c)}</td>' for c in cells)
        color = STATUS_COLORS.get(k.status, STATUS_COLORS["no_data"])
        row += f'<td style="{_CELL};{color};font-weight:600">{_text(k.status)}</td>'
        out.append(f"<tr>{row}</tr>")
    out.append("</table>")

    for k in red:
        owner_name, owner_email = owners[k.kpi_id]
        out.append(f"<h2>{_text(k.name)} (red)</h2>")
        out.append(f"<p>{_text(observation_template(k, ctx.week_start))}</p>")
        reply = replies.get(k.kpi_id)
        if reply:
            out.append(f"<p><strong>Reply from {_text(owner_name)}:</strong></p><dl>")
            for question, attr in zip(
                QUESTIONS, ("what_changed", "why", "owner_of_fix", "actions"), strict=True
            ):
                out.append(
                    f"<dt><strong>{_text(question)}</strong></dt>"
                    f'<dd style="margin:0 0 .75rem 1rem">{_text(getattr(reply, attr))}</dd>'
                )
            out.append("</dl>")
        else:
            out.append(
                f"<p><strong>No reply yet</strong> from {_text(owner_name)} "
                f"({_text(owner_email)}): this KPI is unanswered.</p>"
            )
    out.append("</div>")
    return "".join(out)


_QUOTE_MARKER_LINE = re.compile(r"^<!-- /?owner-quote[^>]*-->\n?", re.M)


def plain_text(packet_md: str) -> str:
    """The checked packet as plain text: the invisible owner-quote markers removed."""
    return _QUOTE_MARKER_LINE.sub("", packet_md)
