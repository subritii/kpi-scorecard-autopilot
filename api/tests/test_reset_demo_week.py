"""scripts/reset_demo_week.py: dev-only reset of one client's week, for demo rehearsal.

Database tests run as the admin role (the script's role) inside a transaction
that is rolled back, on a far-future week that no real run uses.
"""

import importlib.util
import os

import psycopg
import pytest
from psycopg.rows import dict_row

from app.config import REPO_ROOT

spec = importlib.util.spec_from_file_location(
    "reset_demo_week", REPO_ROOT / "scripts" / "reset_demo_week.py"
)
reset_demo_week = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset_demo_week)

WEEK = "2031-01-06"  # a Monday no seeded data or real run uses


@pytest.fixture
def no_db(monkeypatch):
    """Fail the test if the script tries to connect at all."""

    def refuse(*args, **kwargs):
        raise AssertionError("the script connected to the database")

    monkeypatch.setattr(reset_demo_week.psycopg, "connect", refuse)


# --- guards (checked before touching the database) ----------------------------------


def test_refuses_in_live_email_mode(monkeypatch, no_db, capsys):
    monkeypatch.setenv("EMAIL_MODE", "live")
    assert reset_demo_week.main(["--client", "brightline", "--week", WEEK, "--yes"]) == 2
    assert "EMAIL_MODE=live" in capsys.readouterr().err


@pytest.mark.parametrize("week", ["2031-01-07", "not-a-date"])  # a Tuesday; garbage
def test_refuses_a_week_that_is_not_a_monday(monkeypatch, no_db, week):
    monkeypatch.delenv("EMAIL_MODE", raising=False)
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", "me@gmail.com")
    with pytest.raises(SystemExit):
        reset_demo_week.main(["--client", "brightline", "--week", week])


def test_client_and_week_are_required(no_db):
    with pytest.raises(SystemExit):
        reset_demo_week.main([])


# --- plan and reset (admin, rolled back) ----------------------------------------------------


@pytest.fixture
def admin():
    try:
        conn = psycopg.connect(
            os.environ["ADMIN_DATABASE_URL"],
            row_factory=dict_row,
            autocommit=True,
            connect_timeout=2,
        )
    except (KeyError, psycopg.OperationalError):
        pytest.skip("database not available")
    with conn.transaction(force_rollback=True):
        if not conn.execute("SELECT 1 FROM clients WHERE id = 'brightline'").fetchone():
            pytest.skip("seeded data not found; run scripts/seed_db.py")
        yield conn
    conn.close()


def make_full_run(conn, client_id) -> int:
    """A run with a row in every table that hangs off it."""
    run = conn.execute(
        "INSERT INTO weekly_runs (client_id, week_start, state) VALUES (%s, %s, 'rejected')"
        " RETURNING id",
        (client_id, WEEK),
    ).fetchone()["id"]
    kpi = conn.execute(
        "SELECT id FROM kpi_definitions WHERE client_id = %s LIMIT 1", (client_id,)
    ).fetchone()["id"]
    conn.execute("INSERT INTO run_events (run_id, to_state) VALUES (%s, 'collecting')", (run,))
    conn.execute(
        "INSERT INTO owner_responses (run_id, kpi_id, what_changed, why, owner_of_fix, actions)"
        " VALUES (%s, %s, 'a', 'b', 'c', 'd')",
        (run, kpi),
    )
    conn.execute(
        "INSERT INTO approvals (run_id, approver, decision, packet_version)"
        " VALUES (%s, 'x', 'rejected', 0)",
        (run,),
    )
    conn.execute(
        "INSERT INTO link_tokens (jti, run_id, purpose, kpi_id, expires_at)"
        " VALUES (%s, %s, 'reply', %s, now())",
        (f"reset-test-{client_id}", run, kpi),
    )
    conn.execute(
        "INSERT INTO claim_resolutions (run_id, resolution, resolved_by, claim_key)"
        " VALUES (%s, 'not_sent', 'x', 'k')",
        (run,),
    )
    return run


def test_plan_counts_without_deleting(admin):
    run = make_full_run(admin, "brightline")
    plan = reset_demo_week.plan(admin, "brightline", WEEK)
    assert plan["run_id"] == run and plan["state"] == "rejected"
    assert plan["rows"] == {
        "link_tokens": 1,
        "claim_resolutions": 1,
        "run_events": 1,
        "approvals": 1,
        "owner_responses": 1,
        "weekly_runs": 1,
    }
    assert (
        admin.execute("SELECT count(*) AS n FROM run_events WHERE run_id = %s", (run,)).fetchone()[
            "n"
        ]
        == 1
    )


def test_reset_deletes_only_that_clients_run(admin):
    brightline = make_full_run(admin, "brightline")
    northgate = make_full_run(admin, "northgate")  # same week, other client
    kpi_values_before = admin.execute("SELECT count(*) AS n FROM kpi_values").fetchone()["n"]

    deleted = reset_demo_week.reset(admin, brightline)
    assert deleted == reset_demo_week.plan(admin, "northgate", WEEK)["rows"]  # one of each

    assert reset_demo_week.plan(admin, "brightline", WEEK) is None
    assert reset_demo_week.plan(admin, "northgate", WEEK)["run_id"] == northgate
    # KPI values are kept: /runs/start recomputes the week for the new run.
    assert (
        admin.execute("SELECT count(*) AS n FROM kpi_values").fetchone()["n"] == kpi_values_before
    )


def test_plan_for_a_week_with_no_run_is_none(admin):
    assert reset_demo_week.plan(admin, "brightline", WEEK) is None


def test_running_the_script_in_tests_does_not_bring_back_dev_settings(monkeypatch, no_db):
    # main() calls load_env(); afterwards the suite must still be on the test
    # database and in live email mode (it fills only unset variables).
    before = (os.environ["DATABASE_URL"], os.environ.get("TEST_EMAIL_OVERRIDE"))
    reset_demo_week.main(["--client", "brightline", "--week", WEEK])  # live mode: refuses
    assert (os.environ["DATABASE_URL"], os.environ.get("TEST_EMAIL_OVERRIDE")) == before
