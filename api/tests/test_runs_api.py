"""Run lifecycle endpoints: start -> observations -> responses -> packet -> approval -> sent.

Fake LLM only (its replies are set per test). Database work runs as app_user
in one transaction that is rolled back.
"""

import importlib.util
import json
import os
from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row

from app.config import REPO_ROOT
from app.eval_cases import DOCS
from app.faithfulness import OwnerResponse, render_owner_quote
from app.llm.base import LLMError
from app.llm.fake import FakeProvider
from app.main import app, get_connect, get_llm

spec = importlib.util.spec_from_file_location("embed_docs", REPO_ROOT / "scripts" / "embed_docs.py")
embed_docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(embed_docs)

client = TestClient(app, headers={"X-API-Key": os.environ["API_KEY"]})
GROUND_TRUTH = json.loads((REPO_ROOT / "data" / "seed" / "ground_truth.json").read_text())
WEEK_5 = "2026-08-03"  # Brightline: AR over 60 is the only red KPI
CONTROLLER = "controller@ledgerline-cfo.example"
GOOD = "For the week of Aug 3, 2026, AR Over 60 Days rose, led by Ambercrest Logistics."
BAD = "AR Over 60 Days rose because two customers paid late."
REPLY = {
    "what_changed": "Two customers paid late; about $59,000 is overdue.",
    "why": "Both moved to new AP systems.",
    "owner_of_fix": "Avery Chen",
    "actions": "Weekly calls until paid.",
}


def as_client(conn, client_id):
    conn.execute(sql.SQL("SET LOCAL app.current_client_id = {}").format(sql.Literal(client_id)))


@pytest.fixture
def fake():
    return FakeProvider(embedding_dim=int(os.environ.get("EMBEDDING_DIM", "768")), completion=GOOD)


@pytest.fixture
def db(fake, monkeypatch):
    try:
        conn = psycopg.connect(
            os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True, connect_timeout=2
        )
    except (KeyError, psycopg.OperationalError):
        pytest.skip("database not available")
    monkeypatch.setenv("RETRIEVAL_MIN_SIMILARITY", "0.2")  # the fake embedder's scale

    @contextmanager
    def connect(client_id):
        as_client(conn, client_id)
        yield conn

    with conn.transaction(force_rollback=True):
        as_client(conn, "brightline")
        if not conn.execute("SELECT 1 FROM kpi_definitions LIMIT 1").fetchone():
            pytest.skip("seeded data not found; run scripts/seed_db.py")
        embed_docs.sync_docs(conn, fake, DOCS)  # fake embeddings, so retrieval works offline
        app.dependency_overrides[get_connect] = lambda: connect
        app.dependency_overrides[get_llm] = lambda: fake
        try:
            yield conn
        finally:
            app.dependency_overrides.clear()
    conn.close()


def post(path, client_id="brightline", **body):
    return client.post(path, json={"client_id": client_id, **body})


def start(week=WEEK_5, client_id="brightline"):
    resp = post("/runs/start", client_id=client_id, week_start=week)
    assert resp.status_code == 200, resp.text
    return resp.json()


def ar_kpi(run) -> dict:
    return next(k for k in run["red_kpis"] if k["formula_key"] == "ar_over_60")


def events(conn, run_id) -> list[tuple]:
    rows = conn.execute(
        "SELECT from_state, to_state FROM run_events WHERE run_id = %s ORDER BY id", (run_id,)
    ).fetchall()
    return [(r["from_state"], r["to_state"]) for r in rows]


def to_awaiting_approval(reply=True) -> dict:
    run = start()
    if reply:
        post(f"/runs/{run['run_id']}/responses", kpi_id=ar_kpi(run)["kpi_id"], **REPLY)
    assert post(f"/runs/{run['run_id']}/packet").status_code == 200
    return run


# --- start ---------------------------------------------------------------------------


def test_start_creates_run_and_returns_red_kpis_with_owners(db):
    run = start()
    assert run["state"] == "asking" and run["created"] is True
    assert [k["formula_key"] for k in run["red_kpis"]] == ["ar_over_60"]
    ar = ar_kpi(run)
    assert (ar["owner_name"], ar["owner_email"]) == ("Avery Chen", "avery.chen@brightline.example")
    assert ar["value"] == "114983.12" and ar["display_value"] == "$114,983.12"
    assert events(db, run["run_id"]) == [(None, "collecting"), ("collecting", "asking")]


def test_start_is_safe_to_call_twice(db):
    first, second = start(), start()
    assert second["run_id"] == first["run_id"]
    assert second["created"] is False and second["state"] == "asking"
    assert second["red_kpis"] == first["red_kpis"]
    count = db.execute(
        "SELECT count(*) AS n FROM weekly_runs WHERE client_id = 'brightline' AND week_start = %s",
        (WEEK_5,),
    ).fetchone()["n"]
    assert count == 1
    assert len(events(db, first["run_id"])) == 2  # no extra transitions


def test_start_does_not_reset_a_run_that_moved_on(db):
    run = to_awaiting_approval()
    again = start()
    assert (again["run_id"], again["state"]) == (run["run_id"], "awaiting_approval")


# --- observations ------------------------------------------------------------------------


def test_observation_from_llm_when_it_passes(db, fake):
    run = start()
    [msg] = post(f"/runs/{run['run_id']}/observations").json()["messages"]
    assert (msg["source"], msg["attempts"]) == ("llm", 1)
    assert msg["observation"] == GOOD
    assert msg["owner_email"] == "avery.chen@brightline.example"
    assert msg["message"].startswith("Hi Avery,") and "Who owns the fix?" in msg["message"]
    # The prompt came from prompts/ and carried the computed numbers, not raw data.
    prompt = fake.calls[0]["prompt"]
    assert "$114,983.12" in prompt and "Ambercrest Logistics" in prompt


def test_observation_includes_last_years_explanation_verbatim(db):
    run = start()
    [msg] = post(f"/runs/{run['run_id']}/observations").json()["messages"]
    assert msg["past_explanation_doc_key"] == "past_explanation:ar_over_60:2025-08-04"
    assert "Mosswood Hospitality held payments" in msg["message"]


def test_observation_retries_once_with_the_problems(db, fake):
    fake.completion = [BAD, GOOD]
    run = start()
    [msg] = post(f"/runs/{run['run_id']}/observations").json()["messages"]
    assert (msg["source"], msg["attempts"]) == ("llm_retry", 2)
    assert "because" in fake.calls[1]["prompt"]  # the retry says what was wrong
    assert msg["first_attempt_problems"][0]["type"] == "causal"


def test_observation_falls_back_to_template_after_two_failures(db, fake):
    fake.completion = [BAD, BAD]
    run = start()
    [msg] = post(f"/runs/{run['run_id']}/observations").json()["messages"]
    assert (msg["source"], msg["attempts"]) == ("template", 2)
    assert msg["observation"].startswith("For the week of Aug 3, 2026, AR Over 60 Days rose to")
    assert "because" not in msg["message"].split("For reference")[0]


def test_observation_falls_back_to_template_when_the_llm_is_down(db, fake):
    def down(prompt, *, system=None):
        raise LLMError("ollama: could not reach http://localhost:11434/api/chat (ConnectError)")

    fake.complete = down
    run = start()
    [msg] = post(f"/runs/{run['run_id']}/observations").json()["messages"]
    assert msg["source"] == "template"
    assert "could not reach" in msg["llm_error"]


def red_event_weeks():
    for e in GROUND_TRUTH["events"]:
        for w in e["weeks"]:
            expected = next(
                x["expected_status"]
                for x in GROUND_TRUTH["expected_statuses"]
                if (x["client_id"], x["formula_key"], x["week_start"])
                == (e["client_id"], e["formula_key"], w["week_start"])
            )
            if expected == "red":
                yield pytest.param(e, w["week_start"], id=f"{e['formula_key']}-wk{w['week']}")


@pytest.mark.parametrize("event, week", list(red_event_weeks()))
def test_template_fallback_is_faithful_for_every_red_event(db, fake, event, week):
    fake.completion = [BAD, BAD]
    run = start(week, client_id=event["client_id"])
    msgs = post(f"/runs/{run['run_id']}/observations", client_id=event["client_id"]).json()[
        "messages"
    ]
    [msg] = [m for m in msgs if m["formula_key"] == event["formula_key"]]
    assert msg["source"] == "template"
    assert msg["template_check_passed"] is True


# --- responses ------------------------------------------------------------------------------


def test_response_is_saved(db):
    run = start()
    kpi = ar_kpi(run)["kpi_id"]
    resp = post(f"/runs/{run['run_id']}/responses", kpi_id=kpi, **REPLY)
    assert resp.status_code == 200
    saved = db.execute(
        "SELECT what_changed, why FROM owner_responses WHERE run_id = %s", (run["run_id"],)
    ).fetchone()
    assert saved == {"what_changed": REPLY["what_changed"], "why": REPLY["why"]}


def test_response_for_another_clients_kpi_is_404(db):
    run = start()
    as_client(db, "northgate")
    northgate_kpi = db.execute(
        "SELECT id FROM kpi_definitions WHERE client_id = 'northgate' LIMIT 1"
    ).fetchone()["id"]
    assert (
        post(f"/runs/{run['run_id']}/responses", kpi_id=northgate_kpi, **REPLY).status_code == 404
    )


def test_blank_response_is_422(db):
    run = start()
    body = {**REPLY, "why": "   "}
    assert (
        post(f"/runs/{run['run_id']}/responses", kpi_id=ar_kpi(run)["kpi_id"], **body).status_code
        == 422
    )


# --- packet ------------------------------------------------------------------------------------


def test_packet_quotes_reply_verbatim_and_awaits_approval(db):
    run = to_awaiting_approval(reply=True)
    got = client.get(f"/runs/{run['run_id']}", params={"client_id": "brightline"}).json()
    assert got["state"] == "awaiting_approval"
    reply = OwnerResponse(kpi_id=ar_kpi(run)["kpi_id"], **REPLY)
    assert render_owner_quote(reply) in got["packet_md"]
    assert got["unanswered_kpis"] == []


def test_packet_marks_unanswered_kpis(db):
    run = start()
    body = post(f"/runs/{run['run_id']}/packet").json()
    assert body["state"] == "awaiting_approval"
    assert body["faithfulness"]["passed"] is True
    assert [k["formula_key"] for k in body["unanswered_kpis"]] == ["ar_over_60"]
    assert "No reply yet" in body["packet_md"]
    assert "Replies missing for: AR Over 60 Days." in body["packet_md"]


def test_packet_uses_the_latest_reply(db):
    run = start()
    kpi = ar_kpi(run)["kpi_id"]
    post(f"/runs/{run['run_id']}/responses", kpi_id=kpi, **REPLY)
    post(
        f"/runs/{run['run_id']}/responses", kpi_id=kpi, **{**REPLY, "why": "Updated: AP migration."}
    )
    md = post(f"/runs/{run['run_id']}/packet").json()["packet_md"]
    assert "Updated: AP migration." in md and "Both moved to new AP systems." not in md


def test_packet_twice_is_409(db):
    run = to_awaiting_approval()
    assert post(f"/runs/{run['run_id']}/packet").status_code == 409


# --- approval and sending ---------------------------------------------------------------------


def test_only_the_clients_controller_can_approve(db):
    run = to_awaiting_approval()
    resp = post(
        f"/runs/{run['run_id']}/approval", approver="ceo@brightline.example", decision="approved"
    )
    assert resp.status_code == 403


def test_approve_then_mark_sent(db):
    run = to_awaiting_approval()
    rid = run["run_id"]
    assert post(f"/runs/{rid}/mark-sent").status_code == 409  # not approved yet
    approval = post(
        f"/runs/{rid}/approval", approver=CONTROLLER, decision="approved", edits="Looks good."
    )
    assert approval.status_code == 200 and approval.json()["state"] == "awaiting_approval"
    got = client.get(f"/runs/{rid}", params={"client_id": "brightline"}).json()
    assert got["approved"] is True
    assert got["leadership_emails"] == ["ceo@brightline.example", "coo@brightline.example"]
    sent = post(f"/runs/{rid}/mark-sent")
    assert sent.status_code == 200 and sent.json()["state"] == "sent"
    assert post(f"/runs/{rid}/mark-sent").status_code == 409  # already sent
    assert events(db, rid)[-1] == ("awaiting_approval", "sent")


def test_second_approval_is_409(db):
    run = to_awaiting_approval()
    post(f"/runs/{run['run_id']}/approval", approver=CONTROLLER, decision="approved")
    resp = post(f"/runs/{run['run_id']}/approval", approver=CONTROLLER, decision="rejected")
    assert resp.status_code == 409


def test_rejected_packet_cannot_be_sent_until_rebuilt_and_approved(db):
    run = to_awaiting_approval()
    rid = run["run_id"]
    rejected = post(
        f"/runs/{rid}/approval", approver=CONTROLLER, decision="rejected", edits="Fix AR."
    )
    assert rejected.json()["state"] == "rejected"
    assert post(f"/runs/{rid}/mark-sent").status_code == 409
    assert post(f"/runs/{rid}/packet").json()["state"] == "awaiting_approval"  # rebuilt
    assert post(f"/runs/{rid}/mark-sent").status_code == 409  # the old rejection doesn't count
    post(f"/runs/{rid}/approval", approver=CONTROLLER, decision="approved")
    assert post(f"/runs/{rid}/mark-sent").status_code == 200


def test_cannot_skip_from_asking_to_sent(db):
    run = start()
    assert post(f"/runs/{run['run_id']}/mark-sent").status_code == 409
    assert (
        post(
            f"/runs/{run['run_id']}/approval", approver=CONTROLLER, decision="approved"
        ).status_code
        == 409
    )


def test_approvals_are_insert_only(db):
    run = to_awaiting_approval()
    post(f"/runs/{run['run_id']}/approval", approver=CONTROLLER, decision="approved")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with db.transaction():
            db.execute(
                "UPDATE approvals SET decision = 'rejected' WHERE run_id = %s", (run["run_id"],)
            )


# --- needs attention ---------------------------------------------------------------------------


def test_needs_attention_and_resume(db):
    run = start()
    rid = run["run_id"]
    resp = post(f"/runs/{rid}/needs-attention", reason="Gmail node failed")
    assert resp.json()["state"] == "needs_attention"
    assert post(f"/runs/{rid}/packet").json()["state"] == "awaiting_approval"
    note = db.execute(
        "SELECT note FROM run_events WHERE run_id = %s AND to_state = 'needs_attention'", (rid,)
    ).fetchone()["note"]
    assert note == "Gmail node failed"


def test_sent_run_cannot_need_attention(db):
    run = to_awaiting_approval()
    post(f"/runs/{run['run_id']}/approval", approver=CONTROLLER, decision="approved")
    post(f"/runs/{run['run_id']}/mark-sent")
    assert post(f"/runs/{run['run_id']}/needs-attention", reason="x").status_code == 409


# --- isolation and validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, body",
    [
        ("observations", {}),
        ("responses", {"kpi_id": 1, **REPLY}),
        ("packet", {}),
        ("approval", {"approver": CONTROLLER, "decision": "approved"}),
        ("mark-sent", {}),
        ("needs-attention", {"reason": "x"}),
    ],
)
def test_another_clients_run_is_404(db, path, body):
    northgate_run = start(WEEK_5, client_id="northgate")["run_id"]
    assert post(f"/runs/{northgate_run}/{path}", client_id="brightline", **body).status_code == 404


def test_get_run_of_another_client_is_404(db):
    northgate_run = start(WEEK_5, client_id="northgate")["run_id"]
    assert (
        client.get(f"/runs/{northgate_run}", params={"client_id": "brightline"}).status_code == 404
    )


def test_bad_decision_is_422(db):
    run = to_awaiting_approval()
    assert (
        post(f"/runs/{run['run_id']}/approval", approver=CONTROLLER, decision="maybe").status_code
        == 422
    )


# --- TEST_EMAIL_OVERRIDE ------------------------------------------------------------------

ME = "me@gmail.com"
BANNER = "Test mode: originally addressed to "


def recipient_fields(obj, key=None):
    """Every address in a field n8n could send to: `to` lists and *_email(s) fields."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from recipient_fields(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from recipient_fields(v, key)
    elif isinstance(obj, str) and key and (key == "to" or key.endswith(("_email", "_emails"))):
        yield obj


def get_run(run_id, client_id="brightline"):
    return client.get(f"/runs/{run_id}", params={"client_id": client_id}).json()


def test_no_real_address_reaches_a_recipient_field_when_overridden(db, monkeypatch):
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", ME)
    monkeypatch.delenv("EMAIL_MODE")
    run = start()
    rid = run["run_id"]
    responses = [run, post(f"/runs/{rid}/observations").json(), get_run(rid)]
    responses.append(post(f"/runs/{rid}/packet").json())
    post(f"/runs/{rid}/approval", approver=CONTROLLER, decision="approved")
    responses.append(get_run(rid))
    other = start("2026-08-10")  # a second run, for the alert email
    responses.append(post(f"/runs/{other['run_id']}/needs-attention", reason="test").json())

    found = [a for r in responses for a in recipient_fields(r)]
    assert len(found) >= 8  # owner, controller, leadership, reminders, alert
    assert all(a.startswith("me+") and a.endswith("@gmail.com") for a in found), found


def test_owner_email_is_overridden_with_a_banner(db, monkeypatch):
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", ME)
    monkeypatch.delenv("EMAIL_MODE")
    run = start()
    [msg] = post(f"/runs/{run['run_id']}/observations").json()["messages"]
    mail = msg["email"]
    assert mail["to"] == ["me+avery.chen.brightline@gmail.com"]
    assert mail["subject"] == "Week of Aug 3, 2026: AR Over 60 Days needs your input"
    assert mail["body"].startswith(BANNER + "avery.chen@brightline.example\n\n")
    assert mail["body"].endswith(msg["message"])


def test_packet_email_only_after_approval_and_to_leadership(db, monkeypatch):
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", ME)
    monkeypatch.delenv("EMAIL_MODE")
    run = to_awaiting_approval()
    rid = run["run_id"]
    assert get_run(rid)["packet_email"] is None  # nothing to send before approval
    post(f"/runs/{rid}/approval", approver=CONTROLLER, decision="approved")
    got = get_run(rid)
    mail = got["packet_email"]
    assert mail["to"] == ["me+ceo.brightline@gmail.com", "me+coo.brightline@gmail.com"]
    assert mail["subject"] == "KPI packet: Brightline Services, week of Aug 3, 2026"
    assert mail["body"] == (
        BANNER + "ceo@brightline.example, coo@brightline.example\n\n" + got["packet_md"]
    )


def test_reminder_emails_for_unanswered_kpis(db, monkeypatch):
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", ME)
    monkeypatch.delenv("EMAIL_MODE")
    run = start()
    [reminder] = get_run(run["run_id"])["reminder_emails"]
    assert reminder["to"] == ["me+avery.chen.brightline@gmail.com"]
    assert "AR Over 60 Days" in reminder["subject"]
    assert reminder["body"].startswith(BANNER)
    post(f"/runs/{run['run_id']}/responses", kpi_id=ar_kpi(run)["kpi_id"], **REPLY)
    assert get_run(run["run_id"])["reminder_emails"] == []  # answered: no reminder


def test_alert_email_on_needs_attention(db, monkeypatch):
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", ME)
    monkeypatch.delenv("EMAIL_MODE")
    run = start()
    body = post(f"/runs/{run['run_id']}/needs-attention", reason="WF2: Gmail node failed").json()
    alert = body["alert_email"]
    assert alert["to"] == ["me+controller.ledgerline-cfo@gmail.com"]
    assert "WF2: Gmail node failed" in alert["body"]
    assert alert["body"].startswith(BANNER + CONTROLLER)


def test_without_override_emails_go_to_the_real_addresses(db, monkeypatch):
    monkeypatch.delenv("TEST_EMAIL_OVERRIDE", raising=False)
    monkeypatch.setenv("EMAIL_MODE", "live")
    run = start()
    [msg] = post(f"/runs/{run['run_id']}/observations").json()["messages"]
    assert msg["email"]["to"] == ["avery.chen@brightline.example"]
    assert not msg["email"]["body"].startswith(BANNER)
