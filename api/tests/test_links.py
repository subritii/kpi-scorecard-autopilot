# The db and fake fixtures are imported from test_runs_api; ruff reads the test
# parameters that use them as redefinitions (F811), which is how pytest works.
# ruff: noqa: F811
"""Signed, single-use, expiring links: tokens (pure) and the browser pages.

Pages authenticate with the token in the link, not the API key (a browser
can't send headers). Database work runs as app_user and is rolled back.
"""

import os
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_runs_api import (  # noqa: F401  (db and fake are fixtures)
    REPLY,
    ar_kpi,
    client,
    db,
    fake,
    get_run,
    post,
    start,
    to_awaiting_approval,
)

from app import links
from app.main import app

browser = TestClient(app)  # no API key, like a real browser
CONTROLLER = "controller@ledgerline-cfo.example"


def token_of(url: str) -> str:
    return re.search(r"token=([A-Za-z0-9_.\-]+)", url).group(1)


def link_in(mail: dict, page: str) -> str:
    return re.search(rf"\S+/{page}\?token=\S+", mail["body"]).group(0)


def approval_link(rid) -> str:
    mail = post(f"/runs/{rid}/approval-email").json()["approval_email"]
    return link_in(mail, "approve")


def approve(rid, decision="approved", notes=""):
    token = token_of(approval_link(rid))
    return browser.post("/approve", data={"token": token, "decision": decision, "notes": notes})


# --- tokens (pure) ---------------------------------------------------------------------


def test_token_round_trip():
    token = links.sign(
        links.Link(
            "approve",
            run_id=7,
            client_id="brightline",
            packet_version=2,
            kpi_id=None,
            expires=4_102_444_800,
            jti="abc",
        )
    )
    link = links.verify(token)
    assert (link.purpose, link.run_id, link.client_id, link.packet_version) == (
        "approve",
        7,
        "brightline",
        2,
    )


def test_tampered_token_is_rejected():
    token = links.sign(links.Link("approve", 7, "brightline", 2, None, 4_102_444_800, "abc"))
    payload, sig = token.split(".")
    forged = links._b64(links._b64d(payload).replace(b'"r":7', b'"r":8')) + "." + sig
    with pytest.raises(links.InvalidLink):
        links.verify(forged)


def test_token_signed_with_another_secret_is_rejected(monkeypatch):
    token = links.sign(links.Link("reply", 7, "brightline", None, 4, 4_102_444_800, "abc"))
    monkeypatch.setenv("LINK_SECRET", "a-completely-different-secret-" + "x" * 10)
    with pytest.raises(links.InvalidLink):
        links.verify(token)


def test_expired_token_is_rejected():
    token = links.sign(links.Link("reply", 7, "brightline", None, 4, 1, "abc"))  # 1970
    with pytest.raises(links.ExpiredLink):
        links.verify(token)


@pytest.mark.parametrize("garbage", ["", "abc", "a.b", "a.b.c", "!!!.???"])
def test_garbage_token_is_rejected(garbage):
    with pytest.raises(links.InvalidLink):
        links.verify(garbage)


@pytest.mark.parametrize(
    "settings, match",
    [
        ({"LINK_SECRET": "short"}, "LINK_SECRET"),
        ({"PUBLIC_BASE_URL": "localhost:8000"}, "PUBLIC_BASE_URL"),
        ({"LINK_TTL_HOURS": "0"}, "LINK_TTL_HOURS"),
        ({"LINK_TTL_HOURS": "abc"}, "LINK_TTL_HOURS"),
    ],
)
def test_bad_link_settings_stop_the_api_from_starting(monkeypatch, settings, match):
    for k, v in settings.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(links.LinkConfigError, match=match):
        with TestClient(app):
            pass


# --- approval page --------------------------------------------------------------------------


def test_packet_build_returns_an_approval_email_with_a_link(db):
    run = start()
    body = post(f"/runs/{run['run_id']}/packet").json()
    mail = body["approval_email"]
    assert mail["to"] == [CONTROLLER]
    assert "Approve" in mail["subject"]
    url = link_in(mail, "approve")
    assert url.startswith(os.environ["PUBLIC_BASE_URL"] + "/approve?token=")
    assert body["packet_md"] in mail["body"]


def test_opening_the_approval_page_changes_nothing(db):
    run = to_awaiting_approval()
    url = approval_link(run["run_id"])
    path = url.split(os.environ["PUBLIC_BASE_URL"])[1]
    for _ in range(2):  # an email scanner prefetching the link, then the person
        page = browser.get(path)
        assert page.status_code == 200
        assert "Brightline Services" in page.text and "AR Over 60 Days" in page.text
        assert 'value="approved"' in page.text and 'value="rejected"' in page.text
    assert get_run(run["run_id"])["approved"] is False
    assert (
        browser.post(
            "/approve", data={"token": token_of(url), "decision": "approved", "notes": ""}
        ).status_code
        == 200
    )


def test_approving_through_the_link_records_the_controller_from_the_database(db):
    run = to_awaiting_approval()
    resp = approve(run["run_id"], notes="Looks good.")
    assert resp.status_code == 200 and "approved" in resp.text.lower()
    assert get_run(run["run_id"])["approved"] is True
    row = db.execute(
        "SELECT approver, decision, edits FROM approvals WHERE run_id = %s", (run["run_id"],)
    ).fetchone()
    assert row == {"approver": CONTROLLER, "decision": "approved", "edits": "Looks good."}


def test_an_approver_field_in_the_form_is_ignored(db):
    run = to_awaiting_approval()
    token = token_of(approval_link(run["run_id"]))
    browser.post(
        "/approve",
        data={"token": token, "decision": "approved", "approver": "intruder@evil.example"},
    )
    approver = db.execute(
        "SELECT approver FROM approvals WHERE run_id = %s", (run["run_id"],)
    ).fetchone()["approver"]
    assert approver == CONTROLLER


def test_approval_link_works_once(db):
    run = to_awaiting_approval()
    token = token_of(approval_link(run["run_id"]))
    form = {"token": token, "decision": "approved", "notes": ""}
    assert browser.post("/approve", data=form).status_code == 200
    again = browser.post("/approve", data=form)
    assert again.status_code == 410 and "already been used" in again.text


def test_rejecting_through_the_link(db):
    run = to_awaiting_approval()
    resp = approve(run["run_id"], decision="rejected", notes="AR reply is too vague.")
    assert resp.status_code == 200
    assert get_run(run["run_id"])["state"] == "rejected"


def test_link_for_an_older_packet_version_is_refused(db):
    run = to_awaiting_approval()
    rid = run["run_id"]
    old = token_of(approval_link(rid))  # for packet version 1
    approve(rid, decision="rejected", notes="redo")  # uses a different, fresh link
    post(f"/runs/{rid}/packet")  # version 2
    resp = browser.post("/approve", data={"token": old, "decision": "approved", "notes": ""})
    assert resp.status_code == 409 and "changed" in resp.text
    assert get_run(rid)["approved"] is False


def test_expired_approval_link_is_refused(db):
    run = to_awaiting_approval()
    rid = run["run_id"]
    db.execute("SET LOCAL app.current_client_id = 'brightline'")
    run_row = db.execute("SELECT * FROM weekly_runs WHERE id = %s", (rid,)).fetchone()
    url = links.issue(db, run_row, "approve", lifetime=timedelta(seconds=-1))
    resp = browser.post(
        "/approve", data={"token": token_of(url), "decision": "approved", "notes": ""}
    )
    assert resp.status_code == 410 and "expired" in resp.text


@pytest.mark.parametrize("token", ["", "nonsense", "a.b"])
def test_bad_token_on_the_approval_page(db, token):
    assert browser.get("/approve", params={"token": token}).status_code == 403
    assert (
        browser.post("/approve", data={"token": token, "decision": "approved"}).status_code == 403
    )


def test_bad_decision_is_422_and_keeps_the_link_usable(db):
    run = to_awaiting_approval()
    token = token_of(approval_link(run["run_id"]))
    assert browser.post("/approve", data={"token": token, "decision": "maybe"}).status_code == 422
    assert (
        browser.post("/approve", data={"token": token, "decision": "approved"}).status_code == 200
    )


def test_cannot_approve_twice_with_two_links(db):
    run = to_awaiting_approval()
    first, second = token_of(approval_link(run["run_id"])), token_of(approval_link(run["run_id"]))
    browser.post("/approve", data={"token": first, "decision": "approved"})
    resp = browser.post("/approve", data={"token": second, "decision": "rejected"})
    assert resp.status_code == 409


def test_reply_token_cannot_approve(db):
    run = start()
    [msg] = post(f"/runs/{run['run_id']}/observations").json()["messages"]
    reply_token = token_of(link_in(msg["email"], "reply"))
    to_awaiting = post(f"/runs/{run['run_id']}/packet").json()
    assert to_awaiting["state"] == "awaiting_approval"
    assert (
        browser.post("/approve", data={"token": reply_token, "decision": "approved"}).status_code
        == 403
    )


def test_address_based_approval_endpoint_is_gone(db):
    # The interim check that trusted an address in the request is removed.
    run = to_awaiting_approval()
    resp = post(f"/runs/{run['run_id']}/approval", approver=CONTROLLER, decision="approved")
    assert resp.status_code == 404
    assert get_run(run["run_id"])["approved"] is False


def test_pages_send_protective_headers(db):
    run = to_awaiting_approval()
    path = "/approve?token=" + token_of(approval_link(run["run_id"]))
    headers = browser.get(path).headers
    assert headers["cache-control"] == "no-store"
    assert headers["referrer-policy"] == "no-referrer"  # the token is in the URL
    assert headers["x-frame-options"] == "DENY"
    assert "default-src 'none'" in headers["content-security-policy"]
    assert headers["content-type"].startswith("text/html")


def test_page_text_is_escaped():
    from app import pages

    html = pages.page("T", "<p>{}</p>".format(pages.esc("<script>alert(1)</script>")))
    assert "<script>" not in html and "&lt;script&gt;" in html


# --- owner reply form ------------------------------------------------------------------------


def reply_link(run) -> str:
    [msg] = post(f"/runs/{run['run_id']}/observations").json()["messages"]
    return link_in(msg["email"], "reply")


def test_owner_email_carries_a_reply_link(db):
    run = start()
    [msg] = post(f"/runs/{run['run_id']}/observations").json()["messages"]
    url = link_in(msg["email"], "reply")
    assert url.startswith(os.environ["PUBLIC_BASE_URL"] + "/reply?token=")


def test_reply_form_shows_the_kpi_and_four_questions(db):
    run = start()
    page = browser.get("/reply", params={"token": token_of(reply_link(run))})
    assert page.status_code == 200
    assert "AR Over 60 Days" in page.text and "Aug 3, 2026" in page.text
    for field in ("what_changed", "why", "owner_of_fix", "actions"):
        assert f'name="{field}"' in page.text


def test_reply_is_saved_verbatim_and_link_works_once(db):
    run = start()
    token = token_of(reply_link(run))
    text = {**REPLY, "why": "Line one.\nLine two: <b>not html</b> & 100% verbatim."}
    resp = browser.post("/reply", data={"token": token, **text})
    assert resp.status_code == 200
    saved = db.execute(
        "SELECT what_changed, why, owner_of_fix, actions FROM owner_responses WHERE run_id = %s",
        (run["run_id"],),
    ).fetchone()
    assert saved == text
    assert get_run(run["run_id"])["unanswered_kpis"] == []
    again = browser.post("/reply", data={"token": token, **text})
    assert again.status_code == 410


def test_blank_reply_field_redisplays_the_form_and_keeps_the_link(db):
    run = start()
    token = token_of(reply_link(run))
    resp = browser.post("/reply", data={"token": token, **{**REPLY, "why": "  "}})
    assert resp.status_code == 422 and 'name="why"' in resp.text
    assert REPLY["what_changed"] in resp.text  # what they typed is kept
    assert browser.post("/reply", data={"token": token, **REPLY}).status_code == 200


def test_reply_after_the_packet_is_built_is_refused(db):
    run = start()
    token = token_of(reply_link(run))
    post(f"/runs/{run['run_id']}/packet")
    resp = browser.post("/reply", data={"token": token, **REPLY})
    assert resp.status_code == 409


def test_reminders_carry_fresh_reply_links(db):
    run = start()
    reminders = post(f"/runs/{run['run_id']}/reminders").json()["reminder_emails"]
    [mail] = reminders
    token = token_of(link_in(mail, "reply"))
    assert browser.post("/reply", data={"token": token, **REPLY}).status_code == 200
    assert post(f"/runs/{run['run_id']}/reminders").json()["reminder_emails"] == []


# --- ready to send -----------------------------------------------------------------------------


def test_ready_to_send_lists_approved_unclaimed_runs(db):
    run = to_awaiting_approval()
    rid = run["run_id"]

    def ready():
        resp = client.get("/runs/ready-to-send", params={"client_id": "brightline"})
        return [r["run_id"] for r in resp.json()["runs"]]

    assert ready() == []
    approve(rid)
    assert ready() == [rid]
    post(f"/runs/{rid}/claim-send", idempotency_key="n8n-execution-1001")
    assert ready() == []  # claimed: not offered to another execution


# --- HTML packet on the approval page and in the leadership email ------------------------


HOSTILE_REPLY = {
    "what_changed": "<script>alert('x')</script>",
    "why": '<img src=x onerror="alert(1)"> and <b>bold</b>',
    "owner_of_fix": "Avery Chen",
    "actions": "Calls.",
}


def test_approval_page_shows_owner_html_as_text(db):
    run = start()
    post(f"/runs/{run['run_id']}/responses", kpi_id=ar_kpi(run)["kpi_id"], **HOSTILE_REPLY)
    post(f"/runs/{run['run_id']}/packet")
    page = browser.get("/approve", params={"token": token_of(approval_link(run["run_id"]))})
    assert page.status_code == 200
    assert "<script" not in page.text and "<img" not in page.text
    assert "&lt;script&gt;" in page.text and "&lt;b&gt;bold&lt;/b&gt;" in page.text
    assert "<table" in page.text  # rendered HTML, not raw Markdown
    assert "default-src 'none'" in page.headers["content-security-policy"]


def test_leadership_email_has_the_same_html_and_a_plain_text_version(db):
    run = start()
    rid = run["run_id"]
    post(f"/runs/{rid}/responses", kpi_id=ar_kpi(run)["kpi_id"], **HOSTILE_REPLY)
    post(f"/runs/{rid}/packet")
    approve(rid)
    mail = post(f"/runs/{rid}/claim-send", idempotency_key="n8n-execution-1001").json()[
        "packet_email"
    ]
    stored = db.execute(
        "SELECT packet_html, packet_md FROM weekly_runs WHERE id = %s", (rid,)
    ).fetchone()
    assert mail["html"] == stored["packet_html"]  # same HTML as the approval page
    assert "<script" not in mail["html"] and "&lt;script&gt;" in mail["html"]
    assert "<!--" not in mail["body"]  # plain text: no owner-quote markers
    assert "AR Over 60 Days" in mail["body"]


def test_overridden_html_email_gets_the_banner_too(db, monkeypatch):
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", "me@gmail.com")
    monkeypatch.delenv("EMAIL_MODE")
    run = start()
    mail = post(f"/runs/{run['run_id']}/packet").json()["approval_email"]
    assert mail["body"].startswith("Test mode: originally addressed to")
    assert "Test mode: originally addressed to" in mail["html"].split("<table")[0]
