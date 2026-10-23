"""Build the landing page, docs/index.html, from saved dry-run output.

    python scripts/build_demo_site.py

Reads data/demo/example_week.json (written by scripts/capture_demo_data.py from
the real API code, with links scrubbed and fictional .example recipients) and
eval/results.md. No live API calls, no secrets. The output is one
self-contained HTML file (inline CSS and JS, no external requests), ready for
GitHub Pages from /docs. Every value is HTML-escaped; the packet table comes
from the app's own escaped HTML (app/packet.py).

Set the three links below once they exist, then rebuild.
"""

import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "demo" / "example_week.json"
RESULTS = ROOT / "eval" / "results.md"
DOCS_JSON = ROOT / "data" / "seed" / "docs.json"
OUT = ROOT / "docs" / "index.html"

VIDEO_URL = "#"  # VIDEO_URL: the 2-minute demo video
GITHUB_URL = "https://github.com/subritii/kpi-scorecard-autopilot"  # GITHUB_URL: the repository
LINKEDIN_URL = "#"  # LINKEDIN_URL: your profile

esc = html.escape


# --- data helpers -------------------------------------------------------------------------


def compact_money(display: str) -> str:
    """ "$114,983.12" -> "$115K" (for headlines only; the details show exact values)."""
    amount = float(display.replace("$", "").replace(",", "").replace("+", ""))
    return f"${amount / 1000:,.0f}K" if abs(amount) >= 1000 else f"${amount:,.0f}"


def eval_results(markdown: str) -> dict[str, str]:
    """Metric name -> result, from the table in eval/results.md."""
    rows = {}
    for line in markdown.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0] and not cells[0].startswith(("Metric", "---")):
            rows[cells[0]] = cells[2]
    return rows


def metric_chips(results: dict[str, str]) -> list[tuple[str, str]]:
    def first(pattern, text):
        m = re.search(pattern, text)
        return m.group(1) if m else "?"

    by_prefix = lambda prefix: next(v for k, v in results.items() if k.startswith(prefix))  # noqa: E731
    return [
        (first(r"\((\d+/\d+)\)", by_prefix("KPI status")), "KPI statuses right"),
        (first(r"^(\d+)", by_prefix("False alarms")), "false alarms"),
        (first(r"\((\d+/\d+)", by_prefix("Top driver")), "top drivers found"),
        (first(r"^(\d+/\d+)", by_prefix("Packets pass")), "packets pass the fact-check"),
        (first(r"^(\d+)", by_prefix("Cross-client leaks")), "cross-client leaks"),
    ]


def packet_table(packet_html: str) -> str:
    """Just the KPI table out of the packet HTML (already escaped by app/packet.py)."""
    start, end = packet_html.find("<table"), packet_html.find("</table>")
    return packet_html[start : end + len("</table>")] if start >= 0 and end > start else ""


def past_case(doc_key: str | None) -> str | None:
    if not doc_key:
        return None
    docs = json.loads(DOCS_JSON.read_text())
    return next((d["text"] for d in docs if d.get("doc_key") == doc_key), None)


# --- fragments ----------------------------------------------------------------------------

ICONS = {
    "data": '<path d="M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3Z"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    "calc": '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 7h8M8 11h2M12 11h2M16 11h0M8 15h2M12 15h2M8 18h8"/>',
    "alert": '<path d="M12 3 2 20h20L12 3Z"/><path d="M12 10v4M12 17h0"/>',
    "mail": '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 7 9 6 9-6"/>',
    "check": '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="m8 12 3 3 5-6"/>',
    "lock": '<rect x="5" y="10" width="14" height="10" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
    "send": '<path d="M3 11 21 3l-8 18-2-8-8-2Z"/>',
    "shield": '<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3Z"/>',
    "code": '<path d="m8 8-5 4 5 4M16 8l5 4-5 4M14 5l-4 14"/>',
    "person": '<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 4.5-6 8-6s7 2 8 6"/>',
}


def icon(name: str) -> str:
    return (
        '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" fill="none" '
        'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
        f'stroke-linejoin="round">{ICONS[name]}</svg>'
    )


def status_badge(status: str) -> str:
    return f'<span class="badge {esc(status)}">{esc(status)}</span>'


def mini_kpis(kpis: list[dict]) -> str:
    rows = "".join(
        f"<tr><td>{esc(k['name'])}</td><td class='num'>{esc(k['value'])}</td>"
        f"<td class='num muted'>{esc(k['target'])}</td><td>{status_badge(k['status'])}</td></tr>"
        for k in kpis
    )
    return (
        "<table class='mini'><thead><tr><th>KPI</th><th class='num'>This week</th>"
        f"<th class='num'>Target</th><th></th></tr></thead><tbody>{rows}</tbody></table>"
    )


def chips(drivers: list[dict], limit: int = 2) -> str:
    out = []
    for d in drivers[:limit]:
        short = d["name"].split()[0]
        pct = round(float(d["share"].rstrip("%")))
        out.append(f'<span class="chip">{esc(short)} <strong>{pct}%</strong></span>')
    return "".join(out)


def build(data: dict, results_md: str) -> str:
    b, ng, sb = data["brightline"], data["northgate"], data["sandbox"]
    ar = next(k for k in b["kpis"] if k["formula_key"] == "ar_over_60")
    before, after = compact_money(ar["prior"]), compact_money(ar["value"])
    past = past_case(b.get("past_explanation_doc_key"))
    subject = b["owner_email"]["subject"]
    first_name = b["red_kpi"]["owner_name"].split()[0]
    leadership = ", ".join(b["packet_email"]["to"])
    table = packet_table(b["packet_html"])

    steps = [
        (
            "data",
            "Data in",
            "QuickBooks and the sales sheet, every Monday.",
            f"<p>Each client's accounting reports and sales sheet are loaded for the week. "
            f"Sandbox Co's AR aging comes live from a QuickBooks sandbox "
            f"({sb['customers']} customers).</p>",
        ),
        (
            "calc",
            "KPIs computed",
            "Tested code, exact decimals.",
            f"<p class='muted'>{esc(b['client_name'])}, week of {esc(b['week_label'])}</p>{mini_kpis(b['kpis'])}",
        ),
        (
            "alert",
            "Off-target spotted",
            "And what moved it.",
            f"<p><strong>{esc(ar['name'])}</strong> {esc(before)} → {esc(after)} "
            f"{status_badge('red')}</p><div class='chips'>{chips(b['drivers'])}</div>",
        ),
        (
            "mail",
            "Owner asked",
            "A short AI note and a reply form.",
            f"<p class='muted'>To {esc(b['red_kpi']['owner_email'])}</p>"
            f"<p><strong>{esc(subject)}</strong></p><p class='quote'>{esc(b['observation'])}</p>",
        ),
        (
            "check",
            "Packet built",
            "Every AI sentence fact-checked.",
            f"<p>{status_badge('green' if b['packet_passed_check'] else 'red')} "
            f"{'Fact-check passed' if b['packet_passed_check'] else 'Fact-check failed'}</p>"
            f"<div class='packet'>{table}</div>",
        ),
        (
            "lock",
            "Controller approves",
            "Nothing goes out before this.",
            f"<p>Approved by <strong>{esc(b['approval']['approver'])}</strong> on a signed, "
            "single-use link.</p>",
        ),
        (
            "send",
            "Sent once",
            "And logged in ClickUp.",
            f"<p class='muted'>To {esc(leadership)}</p>"
            f"<div class='task'>{icon('check')}<span>{esc(b['clickup_task']['name'])}</span></div>",
        ),
    ]
    step_buttons = "".join(
        f'<li><button class="step{" active" if i == 0 else ""}" data-step="{i}" '
        f'aria-controls="step-panel" aria-pressed="{"true" if i == 0 else "false"}">'
        f'<span class="step-n">{i + 1}</span>{icon(ic)}<span class="step-title">{esc(title)}</span>'
        f'<span class="step-line">{esc(line)}</span></button></li>'
        for i, (ic, title, line, _) in enumerate(steps)
    )
    step_panels = "".join(
        f'<div class="panel" data-panel="{i}"{"" if i == 0 else " hidden"}>{panel}</div>'
        for i, (_, _, _, panel) in enumerate(steps)
    )

    answers = "".join(
        f"<dt>{esc(q)}</dt><dd>{esc(b['reply'][k])}</dd>"
        for q, k in (
            ("What changed?", "what_changed"),
            ("Why?", "why"),
            ("Who owns the fix?", "owner_of_fix"),
            ("What's being done?", "actions"),
        )
    )
    past_html = (
        f"<p class='past'><span class='label'>Last year's similar case</span>{esc(past)}</p>"
        if past
        else ""
    )
    timeline_brightline = f"""
      <ol class="timeline">
        <li><span class="dot red"></span><div>
          <p class="t-label">This week's numbers</p>
          <p class="big">{esc(ar["name"])} <span class="arrow">{esc(before)} → {esc(after)}</span> {status_badge("red")}</p>
          <p class="muted">Target {esc(ar["target"])} or less</p></div></li>
        <li><span class="dot"></span><div>
          <p class="t-label">What moved it</p><div class="chips">{chips(b["drivers"])}</div></div></li>
        <li><span class="dot"></span><div>
          <p class="t-label">{esc(first_name)} gets an email</p>
          <div class="email"><p class="muted">{esc(subject)}</p><p>{esc(b["observation"])}</p>{past_html}
          <p class="link-note">Reply form: four questions →</p></div></div></li>
        <li><span class="dot"></span><div>
          <p class="t-label">{esc(first_name)} answers</p><dl class="answers">{answers}</dl></div></li>
        <li><span class="dot"></span><div>
          <p class="t-label">The packet</p><div class="packet">{table}</div>
          <p class="muted small">Owner answers are quoted word for word. Every AI sentence was fact-checked.</p></div></li>
        <li><span class="dot lock"></span><div>
          <p class="t-label">{icon("lock")} Can't be sent until the Controller approves</p>
          <p>Approved by <strong>{esc(b["approval"]["approver"])}</strong></p></div></li>
        <li><span class="dot green"></span><div>
          <p class="t-label">Sent once</p><p class="muted">To {esc(leadership)}</p>
          <div class="task">{icon("check")}<span>{esc(b["clickup_task"]["name"])}</span></div></div></li>
      </ol>"""
    timeline_northgate = f"""
      <div class="calm"><p class="big">All green: nobody gets bothered.</p>
      <p class="muted">{esc(ng["client_name"])}, week of {esc(ng["week_label"])}</p>{mini_kpis(ng["kpis"])}</div>"""
    sb_ar = sb["kpis"][0]
    timeline_sandbox = f"""
      <div class="calm"><p class="big">Live QuickBooks sandbox data, same checks.</p>
      <p class="muted">{esc(sb["client_name"])}, week of {esc(sb["week_label"])}: {sb["customers"]} customers from the AR aging report</p>
      {mini_kpis(sb["kpis"])}
      <p class="muted small">AR 61-90 days ${esc(sb["totals"]["ar_61_90"])}, 91+ days ${esc(sb["totals"]["ar_90_plus"])}: {esc(sb_ar["name"])} is {esc(sb_ar["status"])}.</p></div>"""

    results_chips = "".join(
        f'<div class="metric"><span class="metric-n">{esc(n)}</span><span>{esc(label)}</span></div>'
        for n, label in metric_chips(eval_results(results_md))
    )

    return TEMPLATE.format(
        video=esc(VIDEO_URL),
        github=esc(GITHUB_URL),
        linkedin=esc(LINKEDIN_URL),
        step_buttons=step_buttons,
        step_panels=step_panels,
        timeline_brightline=timeline_brightline,
        timeline_northgate=timeline_northgate,
        timeline_sandbox=timeline_sandbox,
        brightline=esc(b["client_name"]),
        week=esc(b["week_label"]),
        results=results_chips,
        icon_code=icon("code"),
        icon_alert=icon("alert"),
        icon_lock=icon("lock"),
        icon_shield=icon("shield"),
    )


TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KPI Scorecard Autopilot</title>
<meta name="description" content="Weekly client KPI reports, built, explained and approved automatically.">
<style>
:root {{
  --bg: #f7f7f4; --surface: #ffffff; --text: #1d2327; --muted: #5d6870; --line: #e3e5e0;
  --accent: #2f6f6a; --accent-soft: #e2efed; --red: #b3261e; --red-soft: #fbe3e1;
  --green: #2e6b3a; --green-soft: #e1f1e3; --yellow: #7a5b00; --yellow-soft: #fbf0cc;
  --shadow: 0 1px 2px rgba(0,0,0,.04), 0 6px 24px rgba(0,0,0,.05);
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --bg: #121516; --surface: #1b1f21; --text: #e7ebec; --muted: #9aa6ab; --line: #2a3033;
    --accent: #7cc4bc; --accent-soft: #1f3331; --red: #ff8a80; --red-soft: #3a1f1d;
    --green: #8fd19e; --green-soft: #1d3322; --yellow: #f3d36b; --yellow-soft: #3a3217;
    --shadow: none;
  }}
}}
:root[data-theme="dark"] {{
  --bg: #121516; --surface: #1b1f21; --text: #e7ebec; --muted: #9aa6ab; --line: #2a3033;
  --accent: #7cc4bc; --accent-soft: #1f3331; --red: #ff8a80; --red-soft: #3a1f1d;
  --green: #8fd19e; --green-soft: #1d3322; --yellow: #f3d36b; --yellow-soft: #3a3217;
  --shadow: none;
}}
* {{ box-sizing: border-box; }}
html {{ scroll-behavior: smooth; }}
@media (prefers-reduced-motion: reduce) {{ html {{ scroll-behavior: auto; }} * {{ transition: none !important; }} }}
body {{ margin: 0; background: var(--bg); color: var(--text);
  font: 17px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
a {{ color: var(--accent); }}
.wrap {{ max-width: 1040px; margin: 0 auto; padding: 0 24px; }}
section {{ padding: 112px 0; }}
section + section {{ border-top: 1px solid var(--line); }}
h1 {{ font-size: clamp(2rem, 5vw, 3.3rem); line-height: 1.12; letter-spacing: -.02em; margin: 0 0 20px; max-width: 18ch; }}
h2 {{ font-size: clamp(1.5rem, 3vw, 2.1rem); letter-spacing: -.01em; margin: 0 0 40px; }}
.lead {{ font-size: 1.2rem; color: var(--muted); max-width: 46ch; margin: 0 0 36px; }}
.muted {{ color: var(--muted); }} .small {{ font-size: .9rem; }}
.icon {{ width: 26px; height: 26px; flex: none; }}

/* hero */
.hero {{ padding-top: 96px; }}
.buttons {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 28px; }}
.btn {{ display: inline-block; padding: 12px 20px; border-radius: 999px; text-decoration: none;
  border: 1px solid var(--line); color: var(--text); background: var(--surface); font-weight: 600; }}
.btn.primary {{ background: var(--accent); border-color: var(--accent); color: var(--bg); }}
.fiction {{ display: inline-block; font-size: .85rem; color: var(--muted); border: 1px dashed var(--line);
  padding: 4px 12px; border-radius: 999px; }}
.theme {{ position: fixed; top: 16px; right: 16px; z-index: 5; background: var(--surface); color: var(--text);
  border: 1px solid var(--line); border-radius: 999px; padding: 8px 14px; font: inherit; font-size: .85rem; cursor: pointer; }}

/* problem */
.problem {{ display: grid; grid-template-columns: 1fr auto 1fr auto 1fr; align-items: center; gap: 16px; }}
.card {{ background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 28px;
  box-shadow: var(--shadow); font-weight: 600; font-size: 1.1rem; }}
.card .icon {{ color: var(--accent); display: block; margin-bottom: 14px; }}
.then {{ color: var(--muted); font-size: 1.4rem; }}

/* steps */
.steps {{ list-style: none; padding: 0; margin: 0 0 24px; display: grid; grid-template-columns: repeat(7, 1fr); gap: 10px; }}
.step {{ width: 100%; height: 100%; text-align: left; cursor: pointer; background: var(--surface); color: var(--text);
  border: 1px solid var(--line); border-radius: 14px; padding: 16px 14px; font: inherit;
  display: flex; flex-direction: column; gap: 8px; transition: border-color .2s, transform .2s; }}
.step:hover {{ transform: translateY(-2px); }}
.step.active {{ border-color: var(--accent); background: var(--accent-soft); }}
.step .icon {{ color: var(--accent); }}
.step-n {{ font-size: .75rem; color: var(--muted); font-weight: 700; }}
.step-title {{ font-weight: 700; line-height: 1.25; }}
.step-line {{ font-size: .85rem; color: var(--muted); line-height: 1.35; }}
.panel {{ background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 24px 28px; box-shadow: var(--shadow); }}
.panel p {{ margin: 0 0 12px; }}
.hint {{ font-size: .85rem; color: var(--muted); margin: 0 0 16px; }}

/* tables, badges, chips */
table.mini {{ border-collapse: collapse; width: 100%; font-size: .95rem; }}
table.mini th, table.mini td {{ padding: 8px 10px; border-bottom: 1px solid var(--line); text-align: left; }}
table.mini th {{ color: var(--muted); font-weight: 600; font-size: .8rem; }}
.num {{ text-align: right !important; font-variant-numeric: tabular-nums; }}
.badge {{ display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: .8rem; font-weight: 700; text-transform: uppercase; letter-spacing: .03em; }}
.badge.red {{ background: var(--red-soft); color: var(--red); }}
.badge.green {{ background: var(--green-soft); color: var(--green); }}
.badge.yellow {{ background: var(--yellow-soft); color: var(--yellow); }}
.chips {{ display: flex; flex-wrap: wrap; gap: 8px; }}
.chip {{ background: var(--accent-soft); color: var(--text); border-radius: 999px; padding: 6px 14px; font-size: .95rem; }}
.packet {{ background: #fff; color: #1b1b1b; border-radius: 12px; padding: 12px; overflow-x: auto; border: 1px solid var(--line); }}
.packet table {{ font-size: .85rem; width: 100%; }}
.task {{ display: inline-flex; gap: 10px; align-items: center; background: var(--surface); border: 1px solid var(--line);
  border-left: 4px solid #7b68ee; border-radius: 10px; padding: 10px 14px; font-weight: 600; }}
.task .icon {{ color: #7b68ee; width: 20px; height: 20px; }}
.quote {{ border-left: 3px solid var(--accent); padding-left: 14px; }}

/* timeline */
.pills {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 40px; }}
.pill {{ border: 1px solid var(--line); background: var(--surface); color: var(--text); border-radius: 999px;
  padding: 8px 16px; font: inherit; font-size: .95rem; cursor: pointer; }}
.pill.active {{ background: var(--accent); border-color: var(--accent); color: var(--bg); }}
.timeline {{ list-style: none; margin: 0; padding: 0 0 0 28px; border-left: 2px solid var(--line); max-width: 760px; }}
.timeline > li {{ position: relative; margin: 0 0 44px; }}
.timeline .dot {{ position: absolute; left: -37px; top: 6px; width: 16px; height: 16px; border-radius: 50%;
  background: var(--surface); border: 3px solid var(--accent); }}
.timeline .dot.red {{ border-color: var(--red); }} .timeline .dot.green {{ border-color: var(--green); }}
.t-label {{ font-size: .8rem; text-transform: uppercase; letter-spacing: .06em; color: var(--muted); font-weight: 700;
  margin: 0 0 10px; display: flex; gap: 8px; align-items: center; }}
.t-label .icon {{ width: 18px; height: 18px; }}
.big {{ font-size: 1.3rem; font-weight: 700; margin: 0 0 6px; }}
.arrow {{ font-variant-numeric: tabular-nums; }}
.email {{ background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 20px 22px; box-shadow: var(--shadow); }}
.email p {{ margin: 0 0 12px; }}
.past {{ background: var(--accent-soft); border-radius: 10px; padding: 12px 14px; font-size: .92rem; }}
.past .label {{ display: block; font-weight: 700; font-size: .78rem; text-transform: uppercase; letter-spacing: .05em; color: var(--muted); margin-bottom: 4px; }}
.link-note {{ color: var(--accent); font-weight: 600; margin: 0 !important; }}
.answers {{ margin: 0; display: grid; gap: 4px 20px; grid-template-columns: max-content 1fr; }}
.answers dt {{ font-weight: 700; }} .answers dd {{ margin: 0 0 10px; color: var(--muted); }}
.calm {{ max-width: 760px; }}

/* trust, results, why */
.tiles {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }}
.tile {{ background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 24px; box-shadow: var(--shadow); font-weight: 600; }}
.tile .icon {{ color: var(--accent); display: block; margin-bottom: 14px; }}
.tile .sub {{ display: block; font-weight: 400; color: var(--muted); font-size: .9rem; margin-top: 6px; }}
.metrics {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 14px; margin-bottom: 20px; }}
.metric {{ background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 18px 22px;
  display: flex; flex-direction: column; box-shadow: var(--shadow); }}
.metric-n {{ font-size: 2rem; font-weight: 800; color: var(--accent); font-variant-numeric: tabular-nums; }}
.why p {{ font-size: 1.15rem; max-width: 60ch; margin: 0 0 18px; }}
footer {{ padding: 48px 0 64px; border-top: 1px solid var(--line); color: var(--muted); }}
footer .links {{ display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 10px; }}

/* gentle reveal on scroll (content is visible without JS) */
.js .reveal {{ opacity: 0; transform: translateY(14px); transition: opacity .6s ease, transform .6s ease; }}
.js .reveal.seen {{ opacity: 1; transform: none; }}

@media (max-width: 860px) {{
  section {{ padding: 80px 0; }}
  .steps {{ grid-template-columns: 1fr; }}
  .step {{ flex-direction: row; align-items: center; flex-wrap: wrap; }}
  .step-line {{ flex-basis: 100%; }}
  .problem {{ grid-template-columns: 1fr; }}
  .then {{ transform: rotate(90deg); justify-self: center; }}
  .tiles {{ grid-template-columns: 1fr 1fr; }}
  .answers {{ grid-template-columns: 1fr; }}
}}
@media (max-width: 520px) {{ .tiles {{ grid-template-columns: 1fr; }} body {{ font-size: 16px; }} }}
</style>
</head>
<body>
<button class="theme" id="theme" type="button" aria-label="Switch light or dark mode">Light / dark</button>

<section class="hero"><div class="wrap reveal">
  <h1>Weekly client KPI reports, built, explained and approved automatically.</h1>
  <p class="lead">Numbers come from code, explanations from people, and nothing goes out until a Controller approves it.</p>
  <div class="buttons">
    <a class="btn primary" href="#how">See how it works ↓</a>
    <a class="btn" href="{video}">Watch the 2-min demo</a>
    <a class="btn" href="{github}">GitHub</a>
  </div>
  <span class="fiction">All companies, people and numbers are fictional.</span>
</div></section>

<section><div class="wrap reveal">
  <h2>The weekly grind</h2>
  <div class="problem">
    <div class="card">{icon_code}Numbers pulled by hand for every client</div>
    <span class="then" aria-hidden="true">→</span>
    <div class="card">{icon_alert}Chasing owners to explain red numbers</div>
    <span class="then" aria-hidden="true">→</span>
    <div class="card">{icon_lock}The same report rebuilt every week</div>
  </div>
</div></section>

<section id="how"><div class="wrap reveal">
  <h2>How it works</h2>
  <p class="hint">Tap a step to see it in the example week ({brightline}, week of {week}).</p>
  <ol class="steps">{step_buttons}</ol>
  <div id="step-panel">{step_panels}</div>
</div></section>

<section><div class="wrap reveal">
  <h2>Follow one week</h2>
  <div class="pills" role="tablist">
    <button class="pill active" data-client="brightline" role="tab" aria-selected="true">Brightline Services</button>
    <button class="pill" data-client="northgate" role="tab" aria-selected="false">Northgate Supply</button>
    <button class="pill" data-client="sandbox" role="tab" aria-selected="false">Sandbox Co</button>
  </div>
  <div data-view="brightline">{timeline_brightline}</div>
  <div data-view="northgate" hidden>{timeline_northgate}</div>
  <div data-view="sandbox" hidden>{timeline_sandbox}</div>
</div></section>

<section><div class="wrap reveal">
  <h2>Why you can trust it</h2>
  <div class="tiles">
    <div class="tile">{icon_code}Math in tested code, never the AI</div>
    <div class="tile">{icon_alert}AI says what moved, never why<span class="sub">A fact-checker rejects anything that doesn't match the data.</span></div>
    <div class="tile">{icon_lock}Human approval required, sent exactly once</div>
    <div class="tile">{icon_shield}Each client's data isolated; AI runs locally</div>
  </div>
</div></section>

<section><div class="wrap reveal">
  <h2>Results</h2>
  <div class="metrics">{results}</div>
  <p class="muted small">Measured on synthetic data built around planted events (late payments, a price increase, a seasonal dip, an overstock).</p>
</div></section>

<section class="why"><div class="wrap reveal">
  <h2>Why I built it</h2>
  <p>Fractional CFO firms run the same weekly KPI review for every client, and much of it is copy, paste and chase.</p>
  <p>I wanted to automate that routine with the tools these firms already use (QuickBooks, ClickUp, n8n) while keeping accountants in control.</p>
  <p>Built with Claude Code assistance; I designed the workflow, the tests and the evaluation.</p>
</div></section>

<footer><div class="wrap">
  <div class="links"><a href="{video}">Demo video</a><a href="{github}">GitHub</a><a href="{linkedin}">LinkedIn</a></div>
  <div>All data fictional.</div>
</div></footer>

<script>
(function () {{
  var root = document.documentElement;
  root.classList.add("js");
  // Theme: follows the system unless chosen; ?theme=dark|light also works (screenshots).
  var param = new URLSearchParams(location.search).get("theme");
  var saved = null;
  try {{ saved = localStorage.getItem("theme"); }} catch (e) {{}}
  if (param === "dark" || param === "light") root.dataset.theme = param;
  else if (saved) root.dataset.theme = saved;
  document.getElementById("theme").addEventListener("click", function () {{
    var dark = root.dataset.theme ? root.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try {{ localStorage.setItem("theme", root.dataset.theme); }} catch (e) {{}}
  }});
  // Steps: one panel at a time.
  document.querySelectorAll(".step").forEach(function (btn) {{
    btn.addEventListener("click", function () {{
      document.querySelectorAll(".step").forEach(function (b) {{
        b.classList.toggle("active", b === btn); b.setAttribute("aria-pressed", b === btn);
      }});
      document.querySelectorAll(".panel").forEach(function (p) {{
        p.hidden = p.dataset.panel !== btn.dataset.step;
      }});
    }});
  }});
  // Client pills.
  document.querySelectorAll(".pill").forEach(function (pill) {{
    pill.addEventListener("click", function () {{
      document.querySelectorAll(".pill").forEach(function (p) {{
        p.classList.toggle("active", p === pill); p.setAttribute("aria-selected", p === pill);
      }});
      document.querySelectorAll("[data-view]").forEach(function (v) {{
        v.hidden = v.dataset.view !== pill.dataset.client;
      }});
    }});
  }});
  // Gentle reveal; everything shows at once if IntersectionObserver is missing.
  var items = document.querySelectorAll(".reveal");
  if (!("IntersectionObserver" in window) || new URLSearchParams(location.search).has("static")) {{
    items.forEach(function (el) {{ el.classList.add("seen"); }});
    return;
  }}
  var io = new IntersectionObserver(function (entries) {{
    entries.forEach(function (e) {{ if (e.isIntersecting) {{ e.target.classList.add("seen"); io.unobserve(e.target); }} }});
  }}, {{ threshold: 0.12 }});
  items.forEach(function (el) {{ io.observe(el); }});
}})();
</script>
</body>
</html>
"""


def main() -> int:
    data = json.loads(DATA.read_text())
    page = build(data, RESULTS.read_text())
    OUT.write_text(page)
    print(f"wrote {OUT.relative_to(ROOT)} ({len(page) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
