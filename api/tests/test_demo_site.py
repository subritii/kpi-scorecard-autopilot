"""The landing page build: docs/template.html filled with the saved dry-run data.
Publishable (no tokens, aliases, secrets), only the fields the page uses, and
the embedded JSON can't break out of its script element. No live calls."""

import copy
import importlib.util
import json
import re

import pytest

from app.config import REPO_ROOT

spec = importlib.util.spec_from_file_location(
    "build_demo_site", REPO_ROOT / "scripts" / "build_demo_site.py"
)
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)

DATA = json.loads((REPO_ROOT / "data" / "demo" / "example_week.json").read_text())
TEMPLATE = (REPO_ROOT / "docs" / "template.html").read_text()
RESULTS = (REPO_ROOT / "eval" / "results.md").read_text()


def page(data=DATA, template=TEMPLATE) -> str:
    return site.build(data, template)


def embedded(html: str) -> dict:
    """The JSON the page's script reads (JSON.parse also accepts "<\\/")."""
    m = re.search(r'<script id="demo-data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def test_placeholders_are_filled():
    html = page()
    assert not re.search(r"__[A-Z_]+__", html)
    assert 'href="https://github.com/subritii/kpi-scorecard-autopilot"' in html
    assert embedded(html)["brightline"]["client_name"] == "Brightline Services"


def test_only_the_fields_the_page_uses_are_embedded():
    data = embedded(page())
    b = data["brightline"]
    assert b["owner_email"] == {"subject": DATA["brightline"]["owner_email"]["subject"]}
    for dropped in ("packet_html", "net_change", "observation_source", "past_explanation_doc_key"):
        assert dropped not in b
    assert "description" not in b["clickup_task"] and "subject" not in b["packet_email"]
    assert "models" not in data and "captured_on" not in data
    # Every value the page shows comes straight from the saved data.
    assert b["kpis"][3]["value"] == DATA["brightline"]["kpis"][3]["value"]
    assert b["reply"] == DATA["brightline"]["reply"]


def test_page_is_publishable():
    html = page()
    assert "token=" not in html  # no reply/approval links at all, not even scrubbed ones
    assert "@gmail.com" not in html and not re.search(r"\+[a-z0-9.\-]+@", html)  # no aliases
    assert "ceo@brightline.example" in html  # fictional recipients instead


def test_build_refuses_unpublishable_data():
    bad = copy.deepcopy(DATA)
    bad["brightline"]["reply"]["actions"] = "Reply at http://x/reply?token=abc123def456"
    with pytest.raises(SystemExit, match="link token"):
        page(bad)
    bad = copy.deepcopy(DATA)
    bad["brightline"]["packet_email"]["to"] = ["me+ceo.brightline@gmail.com"]
    with pytest.raises(SystemExit, match="gmail"):
        page(bad)


def test_build_refuses_a_template_with_unknown_placeholders():
    with pytest.raises(SystemExit, match="__NEW_THING__"):
        page(template=TEMPLATE + "<a href='__NEW_THING__'></a>")


def test_embedded_json_cannot_close_the_script_element():
    hostile = copy.deepcopy(DATA)
    hostile["brightline"]["reply"]["why"] = "</script><script>alert(1)</script><!--"
    html = page(hostile)
    assert "</script><script>alert(1)" not in html
    assert embedded(html)["brightline"]["reply"]["why"] == hostile["brightline"]["reply"]["why"]


def test_page_loads_only_google_fonts_from_outside():
    html = page()
    external = re.findall(r"<(?:script|img|iframe|link)[^>]+(?:src|href)=\"(https?://[^\"]+)", html)
    fonts = ("https://fonts.googleapis.com", "https://fonts.gstatic.com")
    assert external and all(u.startswith(fonts) for u in external)
    assert not re.search(r"<script[^>]+src=", html)  # no external scripts


def test_results_shown_match_eval_results():
    """The template writes the results out; they must agree with eval/results.md."""
    html = page()
    for shown, in_results in (
        ("120 of 120", "100% (120/120)"),
        ("10 of 10", "10/10 event weeks"),
        ("7 of 7", "| 7/7 |"),
    ):
        assert shown in html and in_results in RESULTS
    assert "| False alarms across 24 client-weeks | ≤ 1 | 0 |" in RESULTS


def test_saved_data_has_no_live_ids():
    text = (REPO_ROOT / "data" / "demo" / "example_week.json").read_text()
    assert not re.search(r"token=(?!…)[A-Za-z0-9_.\-]{8,}", text)
    assert "@gmail.com" not in text and "demo-realm" not in text
