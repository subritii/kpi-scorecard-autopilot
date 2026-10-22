"""POST /check-faithfulness against real runs built from the seeded data.

Each test runs as app_user in one transaction that is rolled back: it computes
the weeks it needs with /compute, creates a weekly_runs row and an owner reply,
then checks text written from the API's own numbers.
"""

import json
import os
from contextlib import contextmanager
from decimal import Decimal as D

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row

from app.config import REPO_ROOT
from app.faithfulness import OwnerResponse, render_owner_quote
from app.main import app, get_connect

client = TestClient(app)
WEEK_4, WEEK_5, WEEK_6, WEEK_7 = "2026-07-27", "2026-08-03", "2026-08-10", "2026-08-17"


def as_client(conn, client_id):
    conn.execute(sql.SQL("SET LOCAL app.current_client_id = {}").format(sql.Literal(client_id)))


@pytest.fixture
def db():
    try:
        conn = psycopg.connect(
            os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True, connect_timeout=2
        )
    except (KeyError, psycopg.OperationalError):
        pytest.skip("database not available")

    @contextmanager
    def connect(client_id):
        as_client(conn, client_id)
        yield conn

    with conn.transaction(force_rollback=True):
        as_client(conn, "brightline")
        if not conn.execute("SELECT 1 FROM kpi_definitions LIMIT 1").fetchone():
            pytest.skip("seeded data not found; run scripts/seed_db.py")
        app.dependency_overrides[get_connect] = lambda: connect
        try:
            yield conn
        finally:
            app.dependency_overrides.clear()
    conn.close()


def compute(*weeks, client_id="brightline"):
    for w in weeks:
        assert (
            client.post("/compute", json={"client_id": client_id, "week_start": w}).status_code
            == 200
        )


def make_run(conn, week_start, client_id="brightline") -> int:
    as_client(conn, client_id)
    return conn.execute(
        "INSERT INTO weekly_runs (client_id, week_start, state) VALUES (%s, %s, 'assembling')"
        " RETURNING id",
        (client_id, week_start),
    ).fetchone()["id"]


def kpi_id(conn, formula_key, client_id="brightline") -> int:
    as_client(conn, client_id)
    return conn.execute(
        "SELECT id FROM kpi_definitions WHERE client_id = %s AND formula_key = %s",
        (client_id, formula_key),
    ).fetchone()["id"]


def add_owner_reply(conn, run_id, kpi) -> OwnerResponse:
    reply = OwnerResponse(
        kpi_id=kpi,
        what_changed="Two customers paid late; about $59,000 is overdue.",
        why="Both changed AP systems, because of an acquisition.",
        owner_of_fix="Avery Chen",
        actions="Weekly calls until paid.",
    )
    conn.execute(
        "INSERT INTO owner_responses (run_id, kpi_id, what_changed, why, owner_of_fix, actions)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (run_id, kpi, reply.what_changed, reply.why, reply.owner_of_fix, reply.actions),
    )
    return reply


def check(run_id, text, client_id="brightline"):
    return client.post(
        "/check-faithfulness", json={"client_id": client_id, "run_id": run_id, "text": text}
    )


def money(value: str) -> str:
    return f"${D(value):,.2f}"


def ar_sentence(conn, week) -> str:
    """A faithful AR paragraph written from the API's own numbers."""
    ar = kpi_id(conn, "ar_over_60")
    [kpi] = [
        k
        for k in client.post(
            "/compute", json={"client_id": "brightline", "week_start": week}
        ).json()["kpis"]
        if k["kpi_id"] == ar
    ]
    drv = client.post(
        "/drivers", json={"client_id": "brightline", "kpi_id": ar, "week_start": week}
    ).json()
    top = drv["drivers"][0]
    share = (D(top["share_of_movement"]) * 100).quantize(D("0.1"))
    return (
        f"For the week of Aug 3, 2026, AR Over 60 Days rose to {money(kpi['value'])}, up "
        f"{money(drv['net_change'])} from {money(kpi['prior_value'])}. The largest mover was "
        f"{top['name']} (+{money(top['change'])}, {share}% of the movement)."
    )


# --- passing and failing -------------------------------------------------------------


def test_faithful_text_with_owner_quote_passes(db):
    compute(WEEK_4, WEEK_5)
    run = make_run(db, WEEK_5)
    reply = add_owner_reply(db, run, kpi_id(db, "ar_over_60"))
    text = ar_sentence(db, WEEK_5) + "\n\n" + render_owner_quote(reply)

    resp = check(run, text)
    assert resp.status_code == 200
    assert resp.json() == {"client_id": "brightline", "run_id": run, "passed": True, "problems": []}


def test_made_up_number_fails(db):
    compute(WEEK_4, WEEK_5)
    run = make_run(db, WEEK_5)
    body = check(run, "AR Over 60 Days rose to $999,999.99.").json()
    assert body["passed"] is False
    [p] = body["problems"]
    assert set(p) == {"type", "text", "reason", "start"}
    assert (p["type"], p["text"]) == ("number", "$999,999.99")


def test_tampered_owner_quote_fails(db):
    compute(WEEK_4, WEEK_5)
    run = make_run(db, WEEK_5)
    reply = add_owner_reply(db, run, kpi_id(db, "ar_over_60"))
    tampered = render_owner_quote(reply).replace("$59,000", "$9,000")
    body = check(run, tampered).json()
    assert [p["type"] for p in body["problems"]] == ["owner_quote"]


def test_owner_quote_from_another_run_is_not_exempt(db):
    compute(WEEK_4, WEEK_5)
    first, second = make_run(db, WEEK_5), make_run(db, WEEK_4)
    reply = add_owner_reply(db, first, kpi_id(db, "ar_over_60"))
    # The same reply quoted in a different run's packet doesn't match that run's replies.
    assert check(second, render_owner_quote(reply)).json()["passed"] is False


# --- context built from history -----------------------------------------------------


def test_consecutive_red_weeks_come_from_history(db):
    # Brightline AR over 60 is red in weeks 5, 6 and 7.
    compute(WEEK_4, WEEK_5, WEEK_6, WEEK_7)
    run = make_run(db, WEEK_7)
    assert check(run, "AR Over 60 Days has been red for 3 weeks.").json()["passed"] is True
    assert check(run, "AR Over 60 Days has been red for 4 weeks.").json()["passed"] is False


def test_red_kpi_count_comes_from_the_run(db):
    # In week 5 only AR over 60 is red.
    compute(WEEK_4, WEEK_5)
    run = make_run(db, WEEK_5)
    assert check(run, "This week 1 KPI is red.").json()["passed"] is True
    assert check(run, "This week 2 KPIs are red.").json()["passed"] is False


def test_names_come_from_the_runs_drivers(db):
    compute(WEEK_4, WEEK_5)
    run = make_run(db, WEEK_5)
    assert check(run, "AR Over 60 Days rose, led by Ambercrest Logistics.").json()["passed"]
    # A Northgate customer is not in a Brightline run's drivers.
    body = check(run, "AR Over 60 Days rose, led by Valemont Hardware.").json()
    assert [(p["type"], p["text"]) for p in body["problems"]] == [("name", "Valemont Hardware")]


# --- errors -------------------------------------------------------------------------------


def test_other_clients_run_is_404(db):
    compute(WEEK_4, WEEK_5, client_id="northgate")
    northgate_run = make_run(db, WEEK_5, client_id="northgate")
    assert check(northgate_run, "Weekly Revenue rose.", client_id="brightline").status_code == 404


def test_unknown_run_is_404(db):
    assert check(999_999_999, "Weekly Revenue rose.").status_code == 404


def test_run_whose_week_is_not_computed_is_409(db):
    run = make_run(db, "2027-01-04")  # nothing computed for this week
    resp = check(run, "Weekly Revenue rose.")
    assert resp.status_code == 409
    assert "/compute" in resp.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"client_id": "brightline", "run_id": 1, "text": ""},
        {"client_id": "brightline", "run_id": 1},
        {"client_id": "brightline", "run_id": 1, "text": "x", "extra": True},
        {"run_id": 1, "text": "x"},
    ],
)
def test_bad_requests_are_422(body):
    assert client.post("/check-faithfulness", json=body).status_code == 422


# --- every planted event, written from real numbers --------------------------------

GROUND_TRUTH = json.loads((REPO_ROOT / "data" / "seed" / "ground_truth.json").read_text())


def shown(formula_key: str, value: str, signed: bool = False) -> str:
    """How the packet would display a value: % and days to 1 place, money to the cent."""
    v = D(value)
    sign = ("+" if v > 0 else "-" if v < 0 else "") if signed else ""
    v = abs(v) if signed else v
    if formula_key == "gross_margin_pct":
        return f"{sign}{v:.1f}" + (" points" if signed else "%")
    if formula_key == "inventory_days":
        return f"{sign}{v:.1f} days"
    return f"{sign}${v:,.2f}"


def planted_weeks():
    weeks = GROUND_TRUTH["week_starts"]
    for e in GROUND_TRUTH["events"]:
        for w in e["weeks"]:
            prior = weeks[weeks.index(w["week_start"]) - 1]
            yield pytest.param(e, w["week_start"], prior, id=f"{e['formula_key']}-wk{w['week']}")


@pytest.mark.parametrize("event, week_start, prior_week", list(planted_weeks()))
def test_faithful_sentence_passes_for_every_planted_event(db, event, week_start, prior_week):
    cid, key = event["client_id"], event["formula_key"]
    compute(prior_week, week_start, client_id=cid)
    run = make_run(db, week_start, client_id=cid)
    k = kpi_id(db, key, client_id=cid)
    kpi = next(
        x
        for x in client.post("/compute", json={"client_id": cid, "week_start": week_start}).json()[
            "kpis"
        ]
        if x["kpi_id"] == k
    )
    drv = client.post(
        "/drivers", json={"client_id": cid, "kpi_id": k, "week_start": week_start}
    ).json()
    top = drv["drivers"][0]
    share = (D(top["share_of_movement"]) * 100).quantize(D("0.1"))
    verb = "rose" if D(drv["net_change"]) > 0 else "fell"
    # Contributor amounts are money for every KPI here (customers, accounts, categories).
    text = (
        f"{kpi['name']} {verb} to {shown(key, kpi['value'])} "
        f"from {shown(key, kpi['prior_value'])}, "
        f"a change of {shown(key, drv['net_change'], signed=True)}. The largest mover was "
        f"{top['name']} ({shown('money', top['change'], signed=True)}, {share}% of the movement)."
    )
    body = check(run, text, client_id=cid).json()
    assert body["passed"], (text, body["problems"])
