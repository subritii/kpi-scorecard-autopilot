"""Build the landing page, docs/index.html, from docs/template.html and saved dry-run output.

    python scripts/build_demo_site.py

Reads data/demo/example_week.json (written by scripts/capture_demo_data.py from
the real API code, with links scrubbed and fictional .example recipients) and
fills the placeholders in docs/template.html:

  __DEMO_DATA__                 the fields the page uses, as JSON
  __VIDEO_URL__, __GITHUB_URL__, __LINKEDIN_URL__   the links below

No live API calls, no secrets. The page's own script escapes every value it
puts on the page. Set the links below once they exist, then rebuild.
"""

import html
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "demo" / "example_week.json"
TEMPLATE = ROOT / "docs" / "template.html"
OUT = ROOT / "docs" / "index.html"

VIDEO_URL = "#"  # VIDEO_URL: the 2-minute demo video
GITHUB_URL = "https://github.com/subritii/kpi-scorecard-autopilot"  # GITHUB_URL: the repository
LINKEDIN_URL = "#"  # LINKEDIN_URL: your profile

KPI_FIELDS = ("name", "formula_key", "value", "prior", "target", "status")


def pick(record: dict, *fields: str) -> dict:
    return {f: record[f] for f in fields}


def page_data(data: dict) -> dict:
    """Only what docs/template.html reads. Everything else stays out of the page:
    the owner email body (it held the reply link), the packet HTML, the ClickUp
    description, model names and capture metadata."""
    b, n, s = data["brightline"], data["northgate"], data["sandbox"]
    return {
        "brightline": {
            **pick(b, "client_name", "week_label", "reply", "packet_passed_check"),
            "kpis": [pick(k, *KPI_FIELDS) for k in b["kpis"]],
            "red_kpi": pick(b["red_kpi"], "name", "owner_name", "owner_email"),
            "drivers": [pick(d, "name", "change", "share") for d in b["drivers"]],
            "owner_email": pick(b["owner_email"], "subject"),
            # The page shows only the first line of the AI-written note.
            "observation": b["observation"].split("\n")[0],
            "approval": pick(b["approval"], "approver", "decided_at"),
            "packet_email": pick(b["packet_email"], "to"),
            "clickup_task": pick(b["clickup_task"], "name"),
        },
        "northgate": {
            **pick(n, "client_name", "week_label"),
            "kpis": [pick(k, *KPI_FIELDS) for k in n["kpis"]],
        },
        "sandbox": {
            **pick(s, "client_name", "as_of", "customers"),
            "totals": pick(s["totals"], "ar_61_90", "ar_90_plus"),
            "kpis": [pick(k, *KPI_FIELDS) for k in s["kpis"]],
        },
    }


def script_json(value: dict) -> str:
    """JSON that is safe inside <script type="application/json">: a "</" in any
    value could close the script element early, so it is written as "<\\/"
    (still the same JSON string). "<!--" is escaped too, for the same reason."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text.replace("</", "<\\/").replace("<!--", "\\u003c!--")


def assert_publishable(text: str) -> None:
    problems = [
        *(["a link token"] if "token=" in text else []),
        *(["a gmail address"] if "@gmail.com" in text else []),
        *(["a test-mode alias"] if re.search(r"\+[a-z0-9.\-]+@", text) else []),
    ]
    if problems:
        raise SystemExit(f"refusing to build the page: it contains {', '.join(problems)}")


def build(data: dict, template: str) -> str:
    demo = script_json(page_data(data))
    assert_publishable(demo)
    if template.count("__DEMO_DATA__") != 1:
        raise SystemExit("docs/template.html must contain __DEMO_DATA__ exactly once")
    page = template.replace("__DEMO_DATA__", demo)
    for name, url in (
        ("__VIDEO_URL__", VIDEO_URL),
        ("__GITHUB_URL__", GITHUB_URL),
        ("__LINKEDIN_URL__", LINKEDIN_URL),
    ):
        page = page.replace(name, html.escape(url))  # the links sit in href="..." attributes
    leftover = sorted(set(re.findall(r"__[A-Z_]+__", page)))
    if leftover:
        raise SystemExit(f"unfilled placeholders in the template: {', '.join(leftover)}")
    assert_publishable(page)
    return page


def main() -> int:
    page = build(json.loads(DATA.read_text()), TEMPLATE.read_text())
    OUT.write_text(page)
    print(f"wrote {OUT.relative_to(ROOT)} ({len(page) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
