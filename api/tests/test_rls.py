"""Row-level security integration tests against a real Postgres, as app_user.

Skipped when no database is reachable. Each test runs in a transaction that
is rolled back, so nothing is left behind. Test clients use ids that can't
collide with seeded data.
"""

import os

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app.db import client_connection

A, B = "rls_test_a", "rls_test_b"
RLS_TABLES = [
    "clients",
    "kpi_definitions",
    "source_facts",
    "kpi_values",
    "weekly_runs",
    "owner_responses",
    "approvals",
    "doc_chunks",
]


def _connect(**kwargs):
    try:
        return psycopg.connect(
            os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=2, **kwargs
        )
    except (KeyError, psycopg.OperationalError):
        pytest.skip("database not available")


@pytest.fixture
def conn():
    c = _connect()
    yield c
    c.rollback()
    c.close()


def as_client(conn, client_id):
    # Same mechanism as app.db: SET LOCAL, scoped to the current transaction.
    conn.execute(sql.SQL("SET LOCAL app.current_client_id = {}").format(sql.Literal(client_id)))


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


def count(conn, table) -> int:
    return conn.execute(
        sql.SQL("SELECT count(*) AS n FROM {}").format(sql.Identifier(table))
    ).fetchone()["n"]


def test_connected_as_app_user_without_bypass(conn):
    role = conn.execute(
        "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    assert role == {"rolname": "app_user", "rolsuper": False, "rolbypassrls": False}


def test_app_user_owns_no_tables(conn):
    owned = conn.execute(
        "SELECT count(*) AS n FROM pg_tables"
        " WHERE schemaname = 'public' AND tableowner = 'app_user'"
    ).fetchone()["n"]
    assert owned == 0


def test_rls_enabled_and_forced_on_all_tables(conn):
    rows = conn.execute(
        "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
        "WHERE relname = ANY(%s) AND relkind = 'r'",
        (RLS_TABLES,),
    ).fetchall()
    assert {r["relname"] for r in rows} == set(RLS_TABLES)
    assert all(r["relrowsecurity"] and r["relforcerowsecurity"] for r in rows)


def test_missing_setting_returns_zero_rows_not_error(conn):
    """A brand-new session that never set the client sees nothing in any table."""
    setting = conn.execute("SELECT current_setting('app.current_client_id', true) AS v").fetchone()[
        "v"
    ]
    assert setting is None
    for table in RLS_TABLES:
        assert count(conn, table) == 0, table


def test_empty_setting_returns_zero_rows(conn):
    add_client(conn, A)
    as_client(conn, "")
    assert count(conn, "clients") == 0


def test_setting_does_not_leak_past_transaction():
    c = _connect(autocommit=True)
    try:
        with c.transaction():
            as_client(c, "brightline")
            assert (
                c.execute("SELECT current_setting('app.current_client_id') AS v").fetchone()["v"]
                == "brightline"
            )
        # Same session, new transaction: the SET LOCAL value is gone.
        after = c.execute("SELECT current_setting('app.current_client_id', true) AS v").fetchone()
        assert after["v"] in (None, "")
        assert count(c, "clients") == 0
    finally:
        c.close()


# --- Isolation between the two seeded clients --------------------------------

# For each table: the owning client of every row the session can see. The
# run-child tables LEFT JOIN to their run, so a leaked reply/approval whose
# run is hidden would show up as owner NULL instead of silently vanishing.
OWNER_SQL = {
    "clients": "SELECT id AS owner FROM clients",
    "kpi_definitions": "SELECT client_id AS owner FROM kpi_definitions",
    "source_facts": "SELECT client_id AS owner FROM source_facts",
    "kpi_values": "SELECT client_id AS owner FROM kpi_values",
    "weekly_runs": "SELECT client_id AS owner FROM weekly_runs",
    "owner_responses": "SELECT r.client_id AS owner FROM owner_responses o"
    " LEFT JOIN weekly_runs r ON r.id = o.run_id",
    "approvals": "SELECT r.client_id AS owner FROM approvals a"
    " LEFT JOIN weekly_runs r ON r.id = a.run_id",
    "doc_chunks": "SELECT client_id AS owner FROM doc_chunks",
}


@pytest.fixture
def seeded(conn):
    """Seeded Brightline + Northgate, plus one row per client in each table the
    seed script leaves empty, so all 8 tables hold data for both clients."""
    for cid in ("brightline", "northgate"):
        as_client(conn, cid)
        kpi = conn.execute(
            "SELECT id FROM kpi_definitions"
            " WHERE client_id = %s AND formula_key = 'weekly_revenue'",
            (cid,),
        ).fetchone()
        if kpi is None:
            pytest.skip("seeded data not found; run scripts/seed_db.py")
        conn.execute(
            "INSERT INTO kpi_values (client_id, kpi_id, week_start, value, status)"
            " VALUES (%s, %s, '2026-07-06', 1, 'green')",
            (cid, kpi["id"]),
        )
        run = add_run(conn, cid)
        conn.execute(
            "INSERT INTO owner_responses (run_id, kpi_id, why) VALUES (%s, %s, 'x')",
            (run, kpi["id"]),
        )
        conn.execute(
            "INSERT INTO approvals (run_id, approver, decision) VALUES (%s, 'x', 'approved')",
            (run,),
        )
        conn.execute(
            "INSERT INTO doc_chunks (client_id, kpi_id, source_type, text)"
            " VALUES (%s, %s, 'note', 'x')",
            (cid, kpi["id"]),
        )
    return conn


@pytest.mark.parametrize("me", ["brightline", "northgate"])
def test_client_sees_zero_rows_of_the_other_client(seeded, me):
    as_client(seeded, me)
    for table, query in OWNER_SQL.items():
        owners = {r["owner"] for r in seeded.execute(query)}
        # Non-empty check keeps the test honest: zero rows of everything would also "pass".
        assert owners == {me}, f"{table}: visible owners {owners}"


def test_cannot_move_a_row_to_another_client(seeded):
    """UPDATE ... SET client_id = other is blocked by the policy's WITH CHECK."""
    as_client(seeded, "brightline")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        seeded.execute(
            "UPDATE source_facts SET client_id = 'northgate'"
            " WHERE id = (SELECT min(id) FROM source_facts WHERE client_id = 'brightline')"
        )


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
    assert count(conn, "approvals") == 0
    # A also can't attach an approval to B's run.
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "INSERT INTO approvals (run_id, approver, decision) VALUES (%s, 'x', 'approved')",
            (run_b,),
        )


def test_owner_responses_follow_their_runs_client(conn):
    add_client(conn, A)
    add_client(conn, B)
    kpi_b = conn.execute(
        "INSERT INTO kpi_definitions (client_id, name, definition, owner_name, owner_email,"
        " source, formula_key, target, direction)"
        " VALUES (%s, 'Rev', 'd', 'o', 'o@test.example', 'quickbooks', 'weekly_revenue', 1,"
        " 'higher') RETURNING id",
        (B,),
    ).fetchone()["id"]
    run_b = add_run(conn, B)
    conn.execute(
        "INSERT INTO owner_responses (run_id, kpi_id, why) VALUES (%s, %s, 'x')", (run_b, kpi_b)
    )
    as_client(conn, A)
    assert count(conn, "owner_responses") == 0
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute(
            "INSERT INTO owner_responses (run_id, kpi_id, why) VALUES (%s, %s, 'x')",
            (run_b, kpi_b),
        )


def test_app_user_cannot_delete(conn):
    add_client(conn, A)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        conn.execute("DELETE FROM clients WHERE id = %s", (A,))


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


def test_client_connection_sets_client_for_its_transaction():
    _connect().close()  # skip if no database
    with client_connection("brightline") as c:
        setting = c.execute("SELECT current_setting('app.current_client_id') AS v").fetchone()
        assert setting["v"] == "brightline"
    with pytest.raises(ValueError):
        with client_connection(""):
            pass
