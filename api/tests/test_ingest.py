"""POST /ingest/qbo-ar-aging, with the saved real sandbox report (never a live call).

Runs as app_user in one rolled-back transaction, with its own Sandbox client
(realm id below), so it doesn't depend on QBO_REALM_ID in .env.
"""

import copy
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
from app.main import app, get_connect

client = TestClient(app, headers={"X-API-Key": os.environ["API_KEY"]})
[REAL] = json.loads((REPO_ROOT / "data" / "samples" / "qbo_aged_receivables_real.json").read_text())
SANDBOX, REALM = "sandbox_test", "9341450000000001"
WEEK = "2026-10-19"  # the real report is dated 2026-10-22, inside this week


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
            pytest.skip("seeded data not found; run scripts/setup_test_db.py")
        as_client(conn, SANDBOX)
        conn.execute(
            "INSERT INTO clients (id, name, controller_email, leadership_emails, qbo_realm_id)"
            " VALUES (%s, 'Sandbox Test Co', 'controller@ledgerline-cfo.example',"
            " ARRAY['ceo@sandbox-test.example'], %s)",
            (SANDBOX, REALM),
        )
        conn.execute(
            "INSERT INTO kpi_definitions (client_id, name, definition, owner_name, owner_email,"
            " source, formula_key, target, direction)"
            " VALUES (%s, 'AR Over 60 Days', 'd', 'Riley Novak',"
            " 'riley.novak@sandbox-test.example', 'quickbooks', 'ar_over_60', 5000, 'lower')",
            (SANDBOX,),
        )
        app.dependency_overrides[get_connect] = lambda: connect
        try:
            yield conn
        finally:
            app.dependency_overrides.clear()
    conn.close()


def ingest(report=None, client_id=SANDBOX, realm=REALM, week=WEEK):
    return client.post(
        "/ingest/qbo-ar-aging",
        json={
            "client_id": client_id,
            "week_start": week,
            "realm_id": realm,
            "report": copy.deepcopy(REAL) if report is None else report,
        },
    )


def facts(conn, client_id=SANDBOX) -> dict[tuple[str, str], D]:
    as_client(conn, client_id)
    rows = conn.execute(
        "SELECT metric_key, detail_json, value FROM source_facts"
        " WHERE client_id = %s AND week_start = %s AND metric_key IN ('ar_61_90', 'ar_90_plus')",
        (client_id, WEEK),
    ).fetchall()
    return {(r["metric_key"], r["detail_json"]["qbo_id"]): r["value"] for r in rows}


# --- loading ----------------------------------------------------------------------------


def test_ingest_writes_both_buckets_per_customer_and_a_source_load(db):
    resp = ingest()
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["totals"] == {"ar_61_90": "156.00", "ar_90_plus": "0.00"}
    # 17 data rows; 16 customers (Freeman Sporting Goods has two sub-customers).
    assert (body["as_of"], body["rows"], body["customers"]) == ("2026-10-22", 17, 16)
    written = facts(db)
    assert len(written) == 34  # 17 rows x 2 buckets
    assert written[("ar_61_90", "20")] == D("156.00")  # Red Rock Diner
    load = db.execute(
        "SELECT row_count FROM source_loads WHERE client_id = %s AND week_start = %s"
        " AND source = 'qbo_ar_aging'",
        (SANDBOX, WEEK),
    ).fetchone()
    assert load["row_count"] == 34


def test_detail_names_the_top_level_customer_and_job(db):
    ingest()
    as_client(db, SANDBOX)
    detail = db.execute(
        "SELECT detail_json FROM source_facts WHERE client_id = %s AND metric_key = 'ar_61_90'"
        " AND detail_json->>'qbo_id' = '9'",
        (SANDBOX,),
    ).fetchone()["detail_json"]
    assert detail == {"customer": "Freeman Sporting Goods", "job": "55 Twin Lane", "qbo_id": "9"}


def test_reingesting_the_same_report_changes_nothing(db):
    ingest()
    first = facts(db)
    assert ingest().status_code == 200
    assert facts(db) == first


def test_customer_missing_from_a_later_pull_is_zeroed(db):
    ingest()
    later = copy.deepcopy(REAL)
    rows = later["Rows"]["Row"]
    red_rock = next(r for r in rows if r.get("ColData", [{}])[0].get("value") == "Red Rock Diner")
    rows.remove(red_rock)
    grand = rows[-1]["Summary"]["ColData"]
    grand[1]["value"], grand[4]["value"], grand[6]["value"] = "3686.02", "0.00", "5055.52"
    resp = ingest(later)
    assert resp.status_code == 200, resp.text
    assert resp.json()["totals"]["ar_61_90"] == "0.00"
    assert facts(db)[("ar_61_90", "20")] == D("0")  # kept as a row, but zero


def test_computed_kpi_uses_the_ingested_facts(db):
    ingest()
    resp = client.post("/compute", json={"client_id": SANDBOX, "week_start": WEEK})
    [kpi] = resp.json()["kpis"]
    # AR over 60 = 61-90 (156.00) + 90+ (0.00), against a $5,000 target (lower is better).
    assert (kpi["formula_key"], kpi["value"], kpi["status"]) == ("ar_over_60", "156.00", "green")


# --- refusals ------------------------------------------------------------------------------


def test_wrong_realm_is_refused(db):
    resp = ingest(realm="1111111111111111")
    assert resp.status_code == 403
    assert facts(db) == {}


def test_synthetic_clients_cannot_receive_quickbooks_data(db):
    # Brightline and Northgate have no realm id, so no QuickBooks report is accepted for them.
    resp = ingest(client_id="brightline", realm=REALM)
    assert resp.status_code == 403 and "no QuickBooks company" in resp.json()["detail"]


def test_unknown_client_is_404(db):
    assert ingest(client_id="no_such_client").status_code == 404


def test_report_outside_the_week_is_refused(db):
    resp = ingest(week="2026-10-26")  # the report is dated 2026-10-22
    assert resp.status_code == 422 and "2026-10-22" in resp.json()["detail"]


def test_bad_report_is_refused_and_nothing_is_written(db):
    bad = copy.deepcopy(REAL)
    bad["Rows"]["Row"][-1]["Summary"]["ColData"][4]["value"] = "999.00"
    resp = ingest(bad)
    assert resp.status_code == 422 and "grand total" in resp.json()["detail"]
    assert facts(db) == {}


@pytest.mark.parametrize(
    "change",
    [{"week_start": "2026-10-20"}, {"realm_id": ""}, {"report": "not an object"}, {"extra": 1}],
)
def test_bad_requests_are_422(db, change):
    body = {"client_id": SANDBOX, "week_start": WEEK, "realm_id": REALM, "report": REAL, **change}
    assert client.post("/ingest/qbo-ar-aging", json=body).status_code == 422
