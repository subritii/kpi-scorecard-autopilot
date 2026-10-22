"""Load data/seed/ into Postgres.

Runs as the admin role (ADMIN_DATABASE_URL), like migrations: loading data is
an operator task, not app traffic, so it is not subject to RLS. Each client's
rows are still filtered by client_id in code. Everything loads in one
transaction (all or nothing), using upserts only (no deletes), so it is safe
to re-run. Run scripts/generate_synthetic.py and scripts/migrate.py first.
"""

import csv
import json
import os
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from app.config import REPO_ROOT, load_env

SEED_DIR = REPO_ROOT / "data" / "seed"


def load_json(name: str):
    return json.loads((SEED_DIR / name).read_text())


def load_csv(name: str) -> list[dict]:
    with open(SEED_DIR / name, newline="") as f:
        return list(csv.DictReader(f))


def seed_client(conn: psycopg.Connection, client: dict, kpis, facts, loads):
    # The admin role bypasses RLS, so the client_id filters below are what keep
    # each client's rows attached to the right client.
    cid = client["id"]
    conn.execute(
        """
        INSERT INTO clients (id, name, controller_email, leadership_emails, clickup_list_id)
        VALUES (%(id)s, %(name)s, %(controller_email)s, %(leadership_emails)s,
                %(clickup_list_id)s)
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            controller_email = EXCLUDED.controller_email,
            leadership_emails = EXCLUDED.leadership_emails
        """,
        client,
    )

    client_kpis = [k for k in kpis if k["client_id"] == cid]
    conn.cursor().executemany(
        """
        INSERT INTO kpi_definitions (client_id, name, definition, owner_name, owner_email,
                                     source, formula_key, target, direction, yellow_band_pct)
        VALUES (%(client_id)s, %(name)s, %(definition)s, %(owner_name)s, %(owner_email)s,
                %(source)s, %(formula_key)s, %(target)s, %(direction)s, %(yellow_band_pct)s)
        ON CONFLICT (client_id, formula_key) DO UPDATE SET
            name = EXCLUDED.name,
            definition = EXCLUDED.definition,
            owner_name = EXCLUDED.owner_name,
            owner_email = EXCLUDED.owner_email,
            source = EXCLUDED.source,
            target = EXCLUDED.target,
            direction = EXCLUDED.direction,
            yellow_band_pct = EXCLUDED.yellow_band_pct
        """,
        client_kpis,
    )

    client_facts = [
        (
            cid,
            f["week_start"],
            f["metric_key"],
            Decimal(f["value"]),
            Jsonb(json.loads(f["detail_json"])),
        )
        for f in facts
        if f["client_id"] == cid
    ]
    conn.cursor().executemany(
        """
        INSERT INTO source_facts (client_id, week_start, metric_key, value, detail_json)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (client_id, week_start, metric_key, detail_json)
        DO UPDATE SET value = EXCLUDED.value
        """,
        client_facts,
    )
    # One row per report pulled that week, so KPI math can tell an empty report
    # (metric = 0) from one that never arrived (no_data).
    client_loads = [
        (cid, r["week_start"], r["source"], int(r["row_count"]))
        for r in loads
        if r["client_id"] == cid
    ]
    conn.cursor().executemany(
        """
        INSERT INTO source_loads (client_id, week_start, source, row_count)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (client_id, week_start, source) DO UPDATE SET row_count = EXCLUDED.row_count
        """,
        client_loads,
    )
    print(f"{cid}: {len(client_kpis)} KPIs, {len(client_facts)} facts, {len(client_loads)} loads")


def main() -> None:
    load_env()
    kpis = load_json("kpi_definitions.json")
    facts = load_csv("source_facts.csv")
    loads = load_csv("source_loads.csv")
    # Leaving the `with` block commits once; any error rolls back the whole load.
    with psycopg.connect(os.environ["ADMIN_DATABASE_URL"]) as conn:
        for client in load_json("clients.json"):
            seed_client(conn, client, kpis, facts, loads)


if __name__ == "__main__":
    main()
