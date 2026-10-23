"""The landing page build: publishable (no tokens, aliases, secrets, external
resources) and escaped. Uses the saved dry-run data; no live calls."""

import copy
import importlib.util
import json
import re

from app.config import REPO_ROOT

spec = importlib.util.spec_from_file_location(
    "build_demo_site", REPO_ROOT / "scripts" / "build_demo_site.py"
)
site = importlib.util.module_from_spec(spec)
spec.loader.exec_module(site)

DATA = json.loads((REPO_ROOT / "data" / "demo" / "example_week.json").read_text())
RESULTS = (REPO_ROOT / "eval" / "results.md").read_text()


def page(data=DATA) -> str:
    return site.build(data, RESULTS)


def test_page_has_the_story():
    html = page()
    assert "Weekly client KPI reports, built, explained and approved automatically." in html
    assert "All companies, people and numbers are fictional." in html
    for client in ("Brightline Services", "Northgate Supply", "Sandbox Co"):
        assert client in html
    assert "120/120" in html and "10/10" in html  # results come from eval/results.md


def test_page_is_publishable():
    html = page()
    assert not re.search(r"token=(?!…)[A-Za-z0-9_.\-]{8,}", html)  # no reply/approval tokens
    assert "@gmail.com" not in html and not re.search(r"\+[a-z0-9.\-]+@", html)  # no aliases
    assert "ceo@brightline.example" in html  # fictional recipients instead


def test_page_loads_nothing_from_outside():
    html = page()
    assert not re.search(r"<(script|img|iframe|link)[^>]+(src|href)=\"https?://", html, re.I)
    assert "@import" not in html and "url(http" not in html


def test_saved_data_has_no_tokens_aliases_or_live_ids():
    text = (REPO_ROOT / "data" / "demo" / "example_week.json").read_text()
    assert not re.search(r"token=(?!…)[A-Za-z0-9_.\-]{8,}", text)
    assert "@gmail.com" not in text and "demo-realm" not in text


def test_values_are_escaped():
    hostile = copy.deepcopy(DATA)
    hostile["brightline"]["reply"]["why"] = "<script>alert(1)</script>"
    hostile["brightline"]["observation"] = '<img src=x onerror="alert(1)">'
    html = page(hostile)
    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
