"""Row-level security integration tests against a real Postgres.

Skipped when no database is reachable. Each test runs in a transaction that
is rolled back, so nothing is left behind. Test clients use ids that can't
collide with seeded data.
"""

import os

import psycopg
import pytest
from psycopg.rows import dict_row

from app.db import client_connection

A, B = "rls_test_a", "rls_test_b"


def _connect():
    try:
        return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=2)
    except (KeyError, psycopg.OperationalError):
        pytest.skip("database not available")


@pytest.fixture
def conn():
    c = _connect()
    yield c
    c.rollback()
    c.close()


def as_client(conn, client_id):
    conn.execute("SELECT set_config('app.current_client_id', %s, true)", (client_id,))


def add_client(conn, client_id):
    as_client(conn, client_id)
    conn.execute(
        "INSERT INTO clients (id, name, controller_email) VALUES (%s, %s, %s)",
        (client_id, f"Test {client_id}", "c@test.example"),
    )


def add_run(conn, client_id) -> int:
    as_client(conn, client_id)
    return conn.execute(
        "INSERT INTO weekly_runs (client_id, week_start) VALUES (%s, '2026-07-06') RETURNING id",
        (client_id,),
    ).fetchone()["id"]


def test_app_role_cannot_bypass_rls(conn):
    role = conn.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    assert role == {"rolsuper": False, "rolbypassrls": False}


def test_unscoped_connection_sees_nothing(conn):
    add_client(conn, A)
    as_client(conn, "")
    assert conn.execute("SELECT count(*) AS n FROM clients").fetchone()["n"] == 0


def test_client_sees_only_its_own_rows(conn):
    add_client(conn, A)
    add_client(conn, B)
    as_client(conn, A)
    ids = [r["id"] for r in conn.execute("SELECT id FROM clients")]
    assert ids == [A]


def test_cannot_write_another_clients_row(conn):
    add_client(conn, A)
    add_client(conn, B)
    as_client(conn, A)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "INSERT INTO weekly_runs (client_id, week_start) VALUES (%s, '2026-07-06')", (B,)
        )


def test_approvals_follow_their_runs_client(conn):
    add_client(conn, A)
    add_client(conn, B)
    run_b = add_run(conn, B)
    conn.execute(
        "INSERT INTO approvals (run_id, approver, decision) VALUES (%s, 'x', 'approved')",
        (run_b,),
    )
    as_client(conn, A)
    assert conn.execute("SELECT count(*) AS n FROM approvals").fetchone()["n"] == 0
    # A also can't attach an approval to B's run.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "INSERT INTO approvals (run_id, approver, decision) VALUES (%s, 'x', 'approved')",
            (run_b,),
        )


def test_run_cannot_be_sent_without_approval(conn):
    add_client(conn, A)
    run = add_run(conn, A)
    # The inner savepoint rolls back on the error, so the outer test transaction survives.
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.transaction():
            conn.execute("UPDATE weekly_runs SET state = 'sent' WHERE id = %s", (run,))
    conn.execute(
        "INSERT INTO approvals (run_id, approver, decision) VALUES (%s, 'x', 'approved')", (run,)
    )
    conn.execute("UPDATE weekly_runs SET state = 'sent' WHERE id = %s", (run,))


def test_client_connection_is_scoped():
    _connect().close()  # skip if no database
    with client_connection("brightline") as c:
        setting = c.execute("SELECT current_setting('app.current_client_id') AS v").fetchone()
        assert setting["v"] == "brightline"
        c.rollback()
    with pytest.raises(ValueError):
        with client_connection(""):
            pass
