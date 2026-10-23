"""DEV ONLY: delete one client's weekly run so the demo week can be rehearsed again.

    python scripts/reset_demo_week.py --client brightline --week 2026-08-03          # dry run
    python scripts/reset_demo_week.py --client brightline --week 2026-08-03 --yes    # delete

Deletes the run and everything attached to it (link tokens, claim
resolutions, run events, approvals, owner replies) in one transaction. KPI
values are kept: /runs/start recomputes the week for the new run. Source data,
KPI definitions and embedded documents are untouched.

Destructive, so it is guarded:
  - refuses outright if EMAIL_MODE=live (real recipients), before connecting;
  - dry run by default: shows what would be deleted; --yes to delete;
  - runs as the admin role (the app role has no DELETE permission at all).
"""

import argparse
import os
import sys
from datetime import date

import psycopg
from psycopg.rows import dict_row

from app.config import load_env

# Children of weekly_runs, deleted first (foreign keys), then the run itself.
CHILD_TABLES = ("link_tokens", "claim_resolutions", "run_events", "approvals", "owner_responses")


def monday(value: str) -> date:
    try:
        d = date.fromisoformat(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not a date: {value!r}") from e
    if d.isoweekday() != 1:
        raise argparse.ArgumentTypeError(f"{value} is not a Monday (weeks start on Monday)")
    return d


def plan(conn: psycopg.Connection, client_id: str, week_start: date | str) -> dict | None:
    """The run for this client-week and how many rows each table would lose, or None."""
    run = conn.execute(
        "SELECT id, state FROM weekly_runs WHERE client_id = %s AND week_start = %s",
        (client_id, week_start),
    ).fetchone()
    if run is None:
        return None
    rows = {
        table: conn.execute(
            f"SELECT count(*) AS n FROM {table} WHERE run_id = %s",  # table names are constants
            (run["id"],),
        ).fetchone()["n"]
        for table in CHILD_TABLES
    }
    rows["weekly_runs"] = 1
    return {"run_id": run["id"], "state": run["state"], "rows": rows}


def reset(conn: psycopg.Connection, run_id: int) -> dict:
    """Delete the run and its child rows. Returns rows deleted per table."""
    deleted = {
        table: conn.execute(f"DELETE FROM {table} WHERE run_id = %s", (run_id,)).rowcount
        for table in CHILD_TABLES
    }
    deleted["weekly_runs"] = conn.execute(
        "DELETE FROM weekly_runs WHERE id = %s", (run_id,)
    ).rowcount
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--client", required=True, help="client id, e.g. brightline")
    parser.add_argument("--week", required=True, type=monday, help="week start, YYYY-MM-DD")
    parser.add_argument("--yes", action="store_true", help="really delete (default: dry run)")
    args = parser.parse_args(argv)

    load_env()
    if os.environ.get("EMAIL_MODE", "").strip() == "live":
        print(
            "refusing: EMAIL_MODE=live (real recipients). This script is for demo rehearsal only.",
            file=sys.stderr,
        )
        return 2

    with psycopg.connect(os.environ["ADMIN_DATABASE_URL"], row_factory=dict_row) as conn:
        if not conn.execute("SELECT 1 FROM clients WHERE id = %s", (args.client,)).fetchone():
            print(f"no client {args.client!r}", file=sys.stderr)
            return 1
        found = plan(conn, args.client, args.week)
        if found is None:
            print(f"nothing to reset: no run for {args.client}, week of {args.week}")
            return 0
        print(f"{args.client}, week of {args.week}: run {found['run_id']} (state {found['state']})")
        for table, n in found["rows"].items():
            print(f"  {table}: {n} row(s)")
        if not args.yes:
            print("dry run: nothing deleted. Re-run with --yes to delete.")
            return 0
        deleted = reset(conn, found["run_id"])
    # Leaving the `with` block committed the transaction.
    print(f"deleted: {deleted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
