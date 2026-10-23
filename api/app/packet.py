"""Owner-message and packet templates. Pure: built only from computed numbers.

observation_template() is the fallback owner observation when the LLM's text
fails the faithfulness check; assemble() builds the whole packet. Both must
always pass faithfulness.check() (tests/test_packet.py).
"""

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
    owner_name: str, observation: str, past_explanation: tuple[str, str] | None
) -> str:
    """The message sent to a KPI owner. Only `observation` may be LLM-written.

    past_explanation is (week label, text) of last year's similar move, quoted
    verbatim for reference; it never goes through the LLM.
    """
    lines = [f"Hi {owner_name.split()[0]},", "", observation, "", "Please reply with:"]
    lines += [f"{i}. {q}" for i, q in enumerate(QUESTIONS, start=1)]
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
