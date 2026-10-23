"""Endpoint tests for /compute and /drivers.

Database tests run as app_user inside one transaction that is always rolled
back: the route's connection dependency is swapped for that connection, so
/compute really writes kpi_values but nothing is left behind.
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
from app.main import app, get_connect

client = TestClient(app, headers={"X-API-Key": os.environ["API_KEY"]})
GROUND_TRUTH = json.loads((REPO_ROOT / "data" / "seed" / "ground_truth.json").read_text())
TEST_CLIENT = "api_test_client"


@pytest.fixture
def db():
    """app_user connection whose transaction is rolled back at the end of the test."""
    try:
        conn = psycopg.connect(
            os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True, connect_timeout=2
        )
    except (KeyError, psycopg.OperationalError):
        pytest.skip("database not available")

    @contextmanager
    def connect(client_id):
        conn.execute(sql.SQL("SET LOCAL app.current_client_id = {}").format(sql.Literal(client_id)))
        yield conn

    with conn.transaction(force_rollback=True):
        app.dependency_overrides[get_connect] = lambda: connect
        try:
            yield conn
        finally:
            app.dependency_overrides.clear()
    conn.close()


@pytest.fixture
def seeded(db):
    db.execute("SET LOCAL app.current_client_id = 'brightline'")
    if not db.execute("SELECT 1 FROM kpi_definitions LIMIT 1").fetchone():
        pytest.skip("seeded data not found; run scripts/seed_db.py")
    return db


def as_client(conn, client_id):
    conn.execute(sql.SQL("SET LOCAL app.current_client_id = {}").format(sql.Literal(client_id)))


def make_test_client(conn, revenue: str | None, loaded: bool = True) -> int:
    """A one-KPI client with one week of revenue. Returns the KPI id."""
    as_client(conn, TEST_CLIENT)
    conn.execute(
        "INSERT INTO clients (id, name, controller_email) VALUES (%s, 'API Test', 'c@t.example')",
        (TEST_CLIENT,),
    )
    kpi_id = conn.execute(
        "INSERT INTO kpi_definitions (client_id, name, definition, owner_name, owner_email,"
        " source, formula_key, target, direction)"
        " VALUES (%s, 'Revenue', 'd', 'o', 'o@t.example', 'quickbooks', 'weekly_revenue',"
        " 100000, 'higher') RETURNING id",
        (TEST_CLIENT,),
    ).fetchone()["id"]
    if revenue is not None:
        conn.execute(
            "INSERT INTO source_facts (client_id, week_start, metric_key, value, detail_json)"
            " VALUES (%s, '2026-07-06', 'revenue', %s, '{\"customer\": \"X\"}')",
            (TEST_CLIENT, D(revenue)),
        )
    if loaded:
        conn.execute(
            "INSERT INTO source_loads (client_id, week_start, source, row_count)"
            " VALUES (%s, '2026-07-06', 'qbo_profit_and_loss', 1)",
            (TEST_CLIENT,),
        )
    return kpi_id


def kpi_id_for(conn, client_id, formula_key) -> int:
    as_client(conn, client_id)
    return conn.execute(
        "SELECT id FROM kpi_definitions WHERE client_id = %s AND formula_key = %s",
        (client_id, formula_key),
    ).fetchone()["id"]


# --- request validation (no database needed) -----------------------------------


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize(
    "body",
    [
        {"client_id": "brightline", "week_start": "2026-07-07"},  # a Tuesday
        {"client_id": "", "week_start": "2026-07-06"},
        {"week_start": "2026-07-06"},
        {"client_id": "brightline", "week_start": "2026-07-06", "extra": 1},
    ],
)
def test_compute_rejects_bad_requests(body):
    assert client.post("/compute", json=body).status_code == 422


def test_drivers_rejects_bad_limit():
    body = {"client_id": "brightline", "kpi_id": 1, "week_start": "2026-07-06", "limit": 0}
    assert client.post("/drivers", json=body).status_code == 422


# --- Decimal in, strings out ---------------------------------------------------


def test_131000_50_round_trips_unchanged(db):
    kpi_id = make_test_client(db, revenue="131000.50")
    body = {"client_id": TEST_CLIENT, "week_start": "2026-07-06"}

    resp = client.post("/compute", json=body)
    assert resp.status_code == 200
    # In the raw JSON it is a string, so no client can parse it as a float.
    assert '"value":"131000.50"' in resp.text
    [kpi] = resp.json()["kpis"]
    assert (kpi["value"], kpi["display_value"], kpi["status"]) == (
        "131000.50",
        "131000.50",
        "green",
    )

    # Written to kpi_values as exactly the same Decimal.
    stored = db.execute(
        "SELECT value FROM kpi_values WHERE client_id = %s AND kpi_id = %s", (TEST_CLIENT, kpi_id)
    ).fetchone()["value"]
    assert stored == D("131000.50") and str(stored) == "131000.50"

    # And back out through /drivers.
    resp = client.post("/drivers", json={**body, "kpi_id": kpi_id})
    assert resp.json()["drivers"][0]["current"] == "131000.50"


def test_all_numbers_in_responses_are_strings(seeded):
    def walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                yield from walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v)
        else:
            yield obj

    week = {"client_id": "brightline", "week_start": "2026-08-31"}  # week 9: has a prior week
    compute = client.post("/compute", json=week).json()
    drivers = client.post(
        "/drivers", json={**week, "kpi_id": kpi_id_for(seeded, "brightline", "gross_margin_pct")}
    ).json()
    for value in [*walk(compute["kpis"]), *walk(drivers)]:
        # bool is fine (a flag); ints only for ids; never a float.
        assert not isinstance(value, float), value


# --- /compute ------------------------------------------------------------------


def test_compute_report_not_loaded_is_no_data(db):
    make_test_client(db, revenue=None, loaded=False)
    [kpi] = client.post(
        "/compute", json={"client_id": TEST_CLIENT, "week_start": "2026-07-06"}
    ).json()["kpis"]
    assert (kpi["value"], kpi["display_value"], kpi["status"]) == (None, None, "no_data")


def test_compute_unknown_client_is_404(db):
    resp = client.post("/compute", json={"client_id": "no_such_client", "week_start": "2026-07-06"})
    assert resp.status_code == 404


def test_compute_matches_all_120_ground_truth_statuses(seeded):
    expected = {
        (e["client_id"], e["formula_key"], e["week_start"]): e["expected_status"]
        for e in GROUND_TRUTH["expected_statuses"]
    }
    checked = 0
    for client_id in ("brightline", "northgate"):
        for week_start in GROUND_TRUTH["week_starts"]:
            resp = client.post("/compute", json={"client_id": client_id, "week_start": week_start})
            assert resp.status_code == 200
            for kpi in resp.json()["kpis"]:
                assert kpi["status"] == expected[(client_id, kpi["formula_key"], week_start)]
                checked += 1
    assert checked == 120


def test_compute_writes_kpi_values_with_prior_value(seeded):
    client.post("/compute", json={"client_id": "brightline", "week_start": "2026-07-06"})
    week2 = client.post("/compute", json={"client_id": "brightline", "week_start": "2026-07-13"})
    by_key = {k["formula_key"]: k for k in week2.json()["kpis"]}

    as_client(seeded, "brightline")
    rows = seeded.execute(
        "SELECT d.formula_key, v.value, v.prior_value, v.status, v.drivers_json"
        " FROM kpi_values v JOIN kpi_definitions d ON d.id = v.kpi_id"
        " WHERE v.client_id = 'brightline' AND v.week_start = '2026-07-13'"
    ).fetchall()
    assert len(rows) == 5
    for r in rows:
        api = by_key[r["formula_key"]]
        assert (str(r["value"]), str(r["prior_value"]), r["status"]) == (
            api["value"],
            api["prior_value"],
            api["status"],
        )
        assert r["drivers_json"]["drivers"]  # ranked drivers stored alongside


def test_compute_upsert_is_idempotent(seeded):
    body = {"client_id": "brightline", "week_start": "2026-08-03"}
    first = client.post("/compute", json=body).json()
    second = client.post("/compute", json=body).json()
    assert first == second
    count = seeded.execute(
        "SELECT count(*) AS n FROM kpi_values WHERE client_id = 'brightline'"
        " AND week_start = '2026-08-03'"
    ).fetchone()["n"]
    assert count == 5


# --- /drivers ------------------------------------------------------------------


def planted_event_weeks():
    for e in GROUND_TRUTH["events"]:
        for w in e["weeks"]:
            yield pytest.param(e, w["week_start"], id=f"{e['formula_key']}-wk{w['week']}")


@pytest.mark.parametrize("event, week_start", list(planted_event_weeks()))
def test_drivers_endpoint_ranks_planted_cause_first(seeded, event, week_start):
    kpi_id = kpi_id_for(seeded, event["client_id"], event["formula_key"])
    resp = client.post(
        "/drivers",
        json={"client_id": event["client_id"], "kpi_id": kpi_id, "week_start": week_start},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["drivers"][0]["name"] in event["top_drivers"]
    assert body["net_change"] is not None


def test_drivers_shape_for_ratio_kpi(seeded):
    kpi_id = kpi_id_for(seeded, "brightline", "gross_margin_pct")
    body = client.post(
        "/drivers",
        json={"client_id": "brightline", "kpi_id": kpi_id, "week_start": "2026-08-24", "limit": 2},
    ).json()
    assert len(body["drivers"]) == 2
    assert set(body["drivers"][0]) == {"name", "prior", "current", "change", "share_of_movement"}
    # Shares are rounded to 4 places on output only.
    shares = [d["share_of_movement"] for d in body["drivers"]]
    assert all(len(s.split(".")[1]) == 4 for s in shares), shares
    assert body["denominator"]["metric"] == "revenue"
    assert body["denominator"]["exceeds_top_contributor"] is False
    assert body["warnings"] == []


def test_drivers_other_clients_kpi_is_404(seeded):
    northgate_kpi = kpi_id_for(seeded, "northgate", "inventory_days")
    resp = client.post(
        "/drivers",
        json={"client_id": "brightline", "kpi_id": northgate_kpi, "week_start": "2026-08-10"},
    )
    assert resp.status_code == 404
