"""Faithfulness check for AI-written packet text. Pure functions: no database or LLM.

The LLM may describe what moved and which contributors drove it; it may not
invent numbers, names or dates, get the direction wrong, or state causes
(causes only come from KPI owners, rule 2). check() finds each of those.
It is a safety net; the Controller's approval is the final control.

Owner replies are exempt: the packet template inserts them verbatim with
render_owner_quote(), between invisible markers:

    <!-- owner-quote kpi_id=4 -->
    > **What changed:** ...
    > **Why:** ...
    > **Who owns the fix:** ...
    > **What's being done:** ...
    <!-- /owner-quote -->

A block is only exempt if it matches the stored reply exactly, so the LLM
can't hide made-up text inside quote markers.

How the text is checked, in order. Each step masks what it has handled so
later steps don't see it again (e.g. "60" in "AR Over 60 Days" is not a number):
  1. owner quotes   verified against the stored replies, then skipped
  2. causal words   "because", "due to", ... are flagged
  3. dates          must be this run's week start or end, or the prior week's start;
                    ISO week numbers ("Week 32") are not used in packets
  4. known names    KPI names, contributors, the client and people are recognized
  5. numbers        must match a value of the KPI they belong to (see below)
  6. other names    any remaining capitalized name is flagged
  7. direction      "rose"/"fell"/... must match the sign of the change

Numbers belong to a KPI: the nearest KPI or contributor named before them in
the sentence (else the first after), else the KPI in the section heading. The
words just before a number pick which of that KPI's values it may be:

    "from", "last week", "previously", "prior"   the prior value
    "was", "is", "now", "to", "at", ...          the current value
    "up", "down", "rose", "fell", "added", +/-   a change
    "... target" after the number                the target
    "... of the movement" after the number       a contributor's share
    none of these                                current, change, target or share

So the prior value is only allowed next to a comparison word. Whole numbers
may also be real counts: contributors listed, consecutive red weeks, red KPIs.

These are heuristics tuned to the packet's style, and they fail closed: when
unsure they flag. Known limit: lowercase invented names ("marketing spend")
are not detected.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

MASK = "\x00"  # replaces handled text; not a space, so it never looks like a sentence break


# --- inputs ----------------------------------------------------------------------


@dataclass(frozen=True)
class Contributor:
    name: str
    prior: Decimal
    current: Decimal
    change: Decimal
    share_of_movement: Decimal | None  # 0..1


@dataclass(frozen=True)
class Denominator:
    metric: str
    prior: Decimal
    current: Decimal
    change: Decimal


@dataclass(frozen=True)
class KpiContext:
    kpi_id: int
    formula_key: str
    name: str
    direction: str  # "higher" or "lower" is better
    value: Decimal | None
    prior_value: Decimal | None
    target: Decimal
    net_change: Decimal | None
    contributors: tuple[Contributor, ...] = ()
    denominator: Denominator | None = None
    status: str = "green"
    red_streak: int = 0  # consecutive red weeks up to and including this one
    listed_count: int | None = None  # contributors shown in the packet, if fewer than all


@dataclass(frozen=True)
class OwnerResponse:
    kpi_id: int
    what_changed: str
    why: str
    owner_of_fix: str
    actions: str


@dataclass(frozen=True)
class RunContext:
    week_start: date
    client_name: str
    people: tuple[str, ...]  # KPI owners and others the text may name
    kpis: tuple[KpiContext, ...]
    owner_responses: tuple[OwnerResponse, ...] = ()


# --- output ----------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    type: str  # owner_quote | causal | date | week | number | name | direction
    text: str  # the exact text that failed
    reason: str
    start: int  # offset in the checked text


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    problems: list[Problem] = field(default_factory=list)


# --- owner quotes ----------------------------------------------------------------

QUOTE_FIELDS = (
    ("What changed", "what_changed"),
    ("Why", "why"),
    ("Who owns the fix", "owner_of_fix"),
    ("What's being done", "actions"),
)
QUOTE_BLOCK = re.compile(r"<!-- owner-quote kpi_id=(\d+) -->\n.*?\n<!-- /owner-quote -->", re.S)
QUOTE_MARKER = re.compile(r"<!-- /?owner-quote[^>]*-->")


def render_owner_quote(response: OwnerResponse) -> str:
    """The one way an owner reply goes into a packet: verbatim, as a blockquote."""
    lines = [f"<!-- owner-quote kpi_id={response.kpi_id} -->"]
    for label, attr in QUOTE_FIELDS:
        value_lines = getattr(response, attr).splitlines() or [""]
        lines.append(f"> **{label}:** {value_lines[0]}")
        lines += [f"> {line}" for line in value_lines[1:]]
    lines.append("<!-- /owner-quote -->")
    return "\n".join(lines)


# --- vocabulary ------------------------------------------------------------------

# Other ways the text may refer to each KPI (matched case-insensitively).
KPI_ALIASES = {
    "weekly_revenue": ("weekly revenue", "revenue"),
    "gross_margin_pct": ("gross margin %", "gross margin", "margin"),
    "cash_on_hand": ("cash on hand", "cash"),
    "ar_over_60": ("ar over 60 days", "ar over 60", "ar"),
    "new_bookings": ("new bookings", "bookings"),
    "inventory_days": ("inventory days",),
}
PERCENT_KPIS = {"gross_margin_pct"}

CAUSAL = re.compile(
    r"\b(?:because|due\s+to|caused\s+by|as\s+a\s+result\s+of|owing\s+to|thanks\s+to"
    r"|resulted\s+from|attributable\s+to|on\s+account\s+of)\b",
    re.I,
)
# Labels from the KPI definitions, not data: AR aging buckets.
DEFINITION_TERMS = re.compile(r"\b61\s?[-–]\s?90\b|\b90\+|\bover\s+60\s+days\b", re.I)

MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}
MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
DATE_PATTERNS = [
    # 2026-08-03
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), ("y", "m", "d")),
    # Aug 3, 2026 / August 9
    (
        re.compile(rf"\b({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(\d{{4}}))?\b", re.I),
        ("mon", "d", "y"),
    ),
    # 3 Aug 2026
    (re.compile(rf"\b(\d{{1,2}})\s+({MONTH_RE})\.?(?:,?\s+(\d{{4}}))?\b", re.I), ("d", "mon", "y")),
    # 8/3/2026 (US order)
    (re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{4}))?\b"), ("m", "d", "y")),
]
YEAR = re.compile(r"(?<![\w$.,])(?:19|20)\d{2}(?![\w,.%]|\s?[KkMm]\b)")
# Explicit week labels only ("Week 32", "Wk 32"): lowercase "this week 2 KPIs"
# is a count, not a week number.
WEEK = re.compile(r"\b(?:Week|Wk\.?|wk\.?)\s*#?(\d{1,2})\b")

NUMBER = re.compile(
    r"(?<![\w.$])"
    r"(?P<sign>[+\-−])?"
    r"(?P<cur>\$)?"
    r"(?P<int>\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.(?P<frac>\d+))?"
    r"(?:\s?(?P<suffix>[KkMm])(?![A-Za-z]))?"
    r"(?P<pct>\s?%|\s?percent\b|\s+(?:percentage\s+)?(?:points?|pts)\b)?"
)

DIRECTION_WORDS = {
    **dict.fromkeys(
        "rose rise rises rising risen increased increase increases grew grow grows climbed "
        "climbs jumped jumps up".split(),
        "up",
    ),
    **dict.fromkeys(
        "fell fall falls falling fallen decreased decrease decreases declined declines "
        "dropped drops slipped slips down".split(),
        "down",
    ),
    **dict.fromkeys("improved improves improving".split(), "better"),
    **dict.fromkeys("worsened worsens worsening deteriorated".split(), "worse"),
}
DIRECTION_RE = re.compile(r"\b(" + "|".join(DIRECTION_WORDS) + r")\b", re.I)

# Words just before a number that say which kind of value it is.
PRIOR_WORDS = re.compile(
    r"\b(?:from|last\s+week|previously|prior(?:\s+week)?|a\s+week\s+ago|the\s+week\s+before)\b",
    re.I,
)
CURRENT_WORDS = re.compile(
    r"\b(?:was|were|is|are|now|at|to|reached|totaled|totalled|stood|stands)\b", re.I
)
CHANGE_WORDS = re.compile(
    r"\b(?:" + "|".join(DIRECTION_WORDS) + r"|by|added|gained|lost|moved|changed?)\b", re.I
)
SHARE_AFTER = re.compile(r"\s*(?:of\s+(?:the\s+)?(?:movement|change|swing|total)\b|share\b)", re.I)
TARGET_AFTER = re.compile(r"\s*(?:\w+\s+)?(?:target|goal)\b", re.I)

CAPITALIZED = re.compile(r"[A-Z][A-Za-z'’&-]*(?:[ \t]+(?:&[ \t]+)?[A-Z][A-Za-z'’&-]*)*")
# Capitalized words that are fine on their own (not names of companies or accounts).
ALLOWED_WORDS = {
    *(m.capitalize() for m in MONTHS),
    *"Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split(),
    *"AR AP COGS KPI KPIs P&L YoY WoW QoQ Q1 Q2 Q3 Q4 Week Weekly".split(),
}
HEADING = re.compile(r"^#{1,6}[ \t]+.*$", re.M)


# --- helpers ---------------------------------------------------------------------


@dataclass
class Mention:
    """A KPI or contributor named in the text."""

    start: int
    end: int
    kpi: KpiContext | None = None
    contributors: tuple[tuple[KpiContext, Contributor], ...] = ()


def _parse_date(match: re.Match, order: tuple[str, ...]) -> tuple[int | None, int, int] | None:
    parts = dict(zip(order, match.groups(), strict=True))
    month = MONTHS[parts["mon"].lower()] if "mon" in parts else int(parts["m"])
    day = int(parts["d"])
    year = int(parts["y"]) if parts.get("y") else None
    try:
        date(year or 2000, month, day)  # validates (2000 is a leap year, so Feb 29 is OK)
    except ValueError:
        return None
    return year, month, day


def _is_sentence_start(masked: str, pos: int) -> bool:
    i = pos - 1
    while i >= 0 and masked[i] in " \t*_#>`\"'(-•":
        i -= 1
    return i < 0 or masked[i] in ".!?:;\n"


def _value_pools(k: KpiContext) -> dict[str, list[tuple[Decimal, bool]]]:
    """A KPI's citable values by kind, each tagged with whether it is a percentage."""
    pct = k.formula_key in PERCENT_KPIS
    d = k.denominator

    def own(v):
        return [(v, pct)] if v is not None else []

    def den(attr):
        return [(getattr(d, attr), False)] if d else []

    return {
        "current": own(k.value) + [(c.current, False) for c in k.contributors] + den("current"),
        "prior": own(k.prior_value) + [(c.prior, False) for c in k.contributors] + den("prior"),
        "change": own(k.net_change) + [(c.change, False) for c in k.contributors] + den("change"),
        "target": own(k.target),
        "share": [
            (c.share_of_movement * 100, True)
            for c in k.contributors
            if c.share_of_movement is not None
        ],
    }


def _fmt_date(d: date) -> str:
    return f"{d:%b} {d.day}, {d.year}"


# --- the check ---------------------------------------------------------------------


def check(text: str, ctx: RunContext) -> CheckResult:
    masked = text
    problems: list[Problem] = []

    def mask(start: int, end: int) -> None:
        nonlocal masked
        masked = masked[:start] + MASK * (end - start) + masked[end:]

    def flag(kind: str, start: int, end: int, reason: str) -> None:
        problems.append(Problem(kind, text[start:end], reason, start))

    # 1. Owner quotes: verify, then skip their contents.
    replies: dict[int, list[str]] = {}
    for r in ctx.owner_responses:
        replies.setdefault(r.kpi_id, []).append(render_owner_quote(r))
    for m in QUOTE_BLOCK.finditer(text):
        kpi_id = int(m.group(1))
        if kpi_id not in replies:
            flag("owner_quote", m.start(), m.end(), f"no owner reply for KPI {kpi_id} in this run")
        elif m.group() not in replies[kpi_id]:
            flag(
                "owner_quote",
                m.start(),
                m.end(),
                f"owner quote does not match the stored reply for KPI {kpi_id}; "
                "owner replies must be inserted verbatim",
            )
        mask(m.start(), m.end())
    for m in QUOTE_MARKER.finditer(masked):
        flag("owner_quote", m.start(), m.end(), "unclosed or stray owner-quote marker")
        mask(m.start(), m.end())

    # 2. Causal language: only owners explain causes.
    for m in CAUSAL.finditer(masked):
        flag(
            "causal",
            m.start(),
            m.end(),
            f"'{m.group()}' states a cause; only KPI owners explain causes, in their quoted reply",
        )
        mask(m.start(), m.end())

    # 3. Dates: this run's week start or end, or the prior week's start.
    week_end = ctx.week_start + timedelta(days=6)
    prior_week = ctx.week_start - timedelta(weeks=1)
    allowed = {ctx.week_start, week_end, prior_week}
    allowed_md = {(d.month, d.day) for d in allowed}
    date_reason = (
        f"not this run's week ({_fmt_date(ctx.week_start)} to {_fmt_date(week_end)}) "
        f"or the prior week ({_fmt_date(prior_week)})"
    )
    for pattern, order in DATE_PATTERNS:
        for m in pattern.finditer(masked):
            parsed = _parse_date(m, order)
            ok = parsed is not None and (
                (parsed[0] is None and parsed[1:] in allowed_md)
                or (parsed[0] is not None and date(*parsed) in allowed)
            )
            if not ok:
                flag("date", m.start(), m.end(), date_reason)
            mask(m.start(), m.end())
    for m in YEAR.finditer(masked):
        if int(m.group()) not in {d.year for d in allowed}:
            flag("date", m.start(), m.end(), date_reason)
        mask(m.start(), m.end())
    for m in WEEK.finditer(masked):
        flag(
            "week",
            m.start(),
            m.end(),
            f"packets name weeks by date, e.g. 'week of {_fmt_date(ctx.week_start)}', "
            "not by week number",
        )
        mask(m.start(), m.end())

    # 4. Known names: KPIs, contributors, the client, people. Longest first, so
    # "AR Over 60 Days" wins over "AR".
    names: list[tuple[str, KpiContext | None, tuple]] = []
    for k in ctx.kpis:
        for alias in {k.name.lower(), *KPI_ALIASES.get(k.formula_key, ())}:
            names.append((alias, k, ()))
    by_contributor: dict[str, list] = {}
    for k in ctx.kpis:
        for c in k.contributors:
            by_contributor.setdefault(c.name.lower(), []).append((k, c))
    names += [(n, None, tuple(pairs)) for n, pairs in by_contributor.items()]
    names += [(n.lower(), None, ()) for n in (ctx.client_name, *ctx.people)]
    mentions: list[Mention] = []
    for name, kpi, pairs in sorted(names, key=lambda n: len(n[0]), reverse=True):
        for m in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", masked, re.I):
            if kpi or pairs:
                mentions.append(Mention(m.start(), m.end(), kpi, pairs))
            mask(m.start(), m.end())
    # Aging-bucket labels (61-90, 90+) are definitions, not data. After known
    # names, so they can't cut "Over 60 Days" out of the KPI name.
    for m in DEFINITION_TERMS.finditer(masked):
        mask(m.start(), m.end())

    # Sentences, and which KPI each section heading is about.
    breaks = sorted({0, len(text), *(m.end() for m in re.finditer(r"[.!?](?=\s)|\n", masked))})

    def sentence(pos: int) -> tuple[int, int]:
        return max(b for b in breaks if b <= pos), min(b for b in breaks if b > pos)

    sections: list[tuple[int, KpiContext | None]] = []
    for h in HEADING.finditer(text):
        in_heading = [mn for mn in mentions if h.start() <= mn.start < h.end() and mn.kpi]
        best = max(in_heading, key=lambda mn: mn.end - mn.start) if in_heading else None
        sections.append((h.start(), best.kpi if best else None))

    def section_kpi(pos: int) -> KpiContext | None:
        current = [kpi for start, kpi in sections if start <= pos]
        return current[-1] if current else None

    def nearest(start: int, end: int) -> Mention | None:
        """What a phrase is about: the KPI or contributor named nearest before it
        in the sentence, else the first named after it."""
        lo, hi = sentence(start)
        in_sentence = [mn for mn in mentions if lo <= mn.start < hi]
        before = [mn for mn in in_sentence if mn.end <= start]
        after = sorted((mn for mn in in_sentence if mn.start >= end), key=lambda mn: mn.start)
        return max(before, key=lambda mn: mn.end) if before else (after[0] if after else None)

    def kpi_of(mention: Mention | None, pos: int) -> KpiContext | None:
        if mention is None:
            return section_kpi(pos)
        if mention.kpi:
            return mention.kpi
        # A contributor: prefer a KPI named in the same sentence, then the section's.
        lo, hi = sentence(pos)
        preferred = {mn.kpi.kpi_id for mn in mentions if lo <= mn.start < hi and mn.kpi}
        section = section_kpi(pos)
        if section:
            preferred.add(section.kpi_id)
        for k, _ in mention.contributors:
            if k.kpi_id in preferred:
                return k
        return mention.contributors[0][0]

    # 5. Numbers: each must be one of its KPI's values, of the kind its wording implies.
    red_count = sum(1 for k in ctx.kpis if k.status == "red")
    pools = {k.kpi_id: _value_pools(k) for k in ctx.kpis}
    last_number_end: dict[int, int] = {}  # sentence start -> end of its last number so far

    for m in NUMBER.finditer(masked):
        frac = m.group("frac") or ""
        scale = {"k": 1000, "m": 1_000_000}.get((m.group("suffix") or "").lower(), 1)
        written = Decimal(m.group("int").replace(",", "") + (f".{frac}" if frac else "")) * scale
        half_unit = Decimal(1).scaleb(-len(frac)) * scale / 2
        is_pct = bool(m.group("pct"))
        sign = {"+": 1, "-": -1, "−": -1}.get(m.group("sign") or "", 0)
        is_count = not (frac or scale != 1 or is_pct or sign or m.group("cur"))

        about = nearest(m.start(), m.end())
        kpi = kpi_of(about, m.start())
        # The words leading up to the number, since the last number or name.
        lo, _ = sentence(m.start())
        lead_from = max(lo, last_number_end.get(lo, lo))
        if about and about.end <= m.start():
            lead_from = max(lead_from, about.end)
        lead_in = masked[lead_from : m.start()]
        after = masked[m.end() : m.end() + 40]
        last_number_end[lo] = m.end()

        if sign:
            kinds = ["change"]
        elif SHARE_AFTER.match(after):
            kinds = ["share"]
        elif TARGET_AFTER.match(after):
            kinds = ["target"]
        elif PRIOR_WORDS.search(lead_in):
            kinds = ["prior"]
        else:
            # Whichever of a current-value word or a change word is closer decides.
            cur = [w.end() for w in CURRENT_WORDS.finditer(lead_in)]
            chg = [w.end() for w in CHANGE_WORDS.finditer(lead_in)]
            if cur and (not chg or max(cur) > max(chg)):
                kinds = ["current"]
            elif chg:
                kinds = ["change"]
            else:
                kinds = ["current", "change", "target", "share"]

        def matches(pool, signed, n=written, tol=half_unit, pct=is_pct, s=sign):
            """Pool values this number could be (sign-aware for signed numbers)."""
            want = s * n if signed else n
            return [
                v
                for v, v_pct in pool
                if v_pct == pct and abs((v if signed else abs(v)) - want) <= tol
            ]

        counts = [red_count]
        if kpi:
            counts += [len(kpi.contributors), kpi.red_streak]
            if kpi.listed_count is not None:
                counts.append(kpi.listed_count)
        if is_count and written in counts:
            continue
        if kpi is None:
            flag(
                "number",
                m.start(),
                m.end(),
                f"{m.group()} is not tied to a KPI: name the KPI in the same sentence "
                "or section heading",
            )
            continue
        own = pools[kpi.kpi_id]
        pool = [item for kind in kinds for item in own[kind]]
        if matches(pool, signed=bool(sign)):
            continue
        if sign and matches(pool, signed=False):
            flag("direction", m.start(), m.end(), f"{m.group()} has the wrong sign for that change")
            continue
        # Explain the miss as precisely as possible.
        other_kinds = [kd for kd in own if kd not in kinds and matches(own[kd], signed=False)]
        other_kpis = [
            k.name
            for k in ctx.kpis
            if k.kpi_id != kpi.kpi_id
            and any(matches(p, signed=False) for p in pools[k.kpi_id].values())
        ]
        if "prior" in other_kinds:
            reason = (
                f"{m.group()} matches {kpi.name}'s prior value; cite it with a comparison "
                "word such as 'from' or 'last week'"
            )
        elif other_kinds:
            reason = (
                f"{m.group()} matches {kpi.name}'s {other_kinds[0]}, but the wording "
                f"describes its {' or '.join(kinds)}"
            )
        elif other_kpis:
            reason = f"{m.group()} matches {other_kpis[0]}, not {kpi.name}"
        else:
            reason = (
                f"{m.group()} is not in this run's numbers for {kpi.name} "
                f"(within ±{half_unit.normalize():f}, the precision written)"
            )
        flag("number", m.start(), m.end(), reason)

    for m in NUMBER.finditer(masked):
        mask(m.start(), m.end())

    # 6. Any capitalized name still left is unknown.
    for m in CAPITALIZED.finditer(masked):
        words = m.group().split()
        # At a sentence start the first word is capitalized anyway: judge the rest.
        rest = words[1:] if _is_sentence_start(masked, m.start()) else words
        if all(w in ALLOWED_WORDS for w in rest):
            continue
        flag(
            "name",
            m.start(),
            m.end(),
            f"'{m.group()}' is not a contributor in this run's drivers, a KPI, the client, "
            "or a known person",
        )

    # 7. Direction words must match the sign of what they describe.
    for v in DIRECTION_RE.finditer(masked):
        about = nearest(v.start(), v.end())
        word = v.group()
        meaning = DIRECTION_WORDS[word.lower()]
        if about is None:
            kpi = section_kpi(v.start())
            if kpi is None:
                continue
            label, change, span = kpi.name, kpi.net_change, (v.start(), v.end())
        else:
            span = (min(about.start, v.start()), max(about.end, v.end()))
            label = text[about.start : about.end]
            if about.kpi:
                kpi, change = about.kpi, about.kpi.net_change
            elif meaning in ("better", "worse"):
                continue  # "better" for a contributor depends on which KPI; don't guess
            else:
                kpi = kpi_of(about, v.start())
                change = next(c for k, c in about.contributors if k.kpi_id == kpi.kpi_id).change
        if change is None:
            continue
        if meaning in ("better", "worse"):
            up_is_good = kpi.direction == "higher"
            meaning = "up" if (meaning == "better") == up_is_good else "down"
        actual = "up" if change > 0 else "down" if change < 0 else "flat"
        if actual != meaning:
            described = {"up": "rose", "down": "fell", "flat": "did not change"}[actual]
            flag("direction", *span, f"says '{word}' but {label} {described} ({change:+})")

    problems.sort(key=lambda p: (p.start, p.type))
    return CheckResult(passed=not problems, problems=problems)
