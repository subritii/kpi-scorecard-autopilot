"""Load data/seed/ into Postgres.

Seeds through the app role one client at a time, so row-level security is
enforced even here: each client's rows are written on a connection scoped to
that client. Upserts only (no deletes), so it is safe to re-run.
Run scripts/generate_synthetic.py and scripts/migrate.py first.
"""

import csv
import json
from decimal import Decimal

from psycopg.types.json import Jsonb

from app.config import REPO_ROOT, load_env
from app.db import client_connection

SEED_DIR = REPO_ROOT / "data" / "seed"


def load_json(name: str):
    return json.loads((SEED_DIR / name).read_text())


def load_facts() -> list[dict]:
    with open(SEED_DIR / "source_facts.csv", newline="") as f:
        return list(csv.DictReader(f))


def seed_client(client: dict, kpis: list[dict], facts: list[dict]) -> None:
    cid = client["id"]
    with client_connection(cid) as conn:
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
    print(f"{cid}: {len(client_kpis)} KPIs, {len(client_facts)} facts")


def main() -> None:
    load_env()
    kpis = load_json("kpi_definitions.json")
    facts = load_facts()
    for client in load_json("clients.json"):
        seed_client(client, kpis, facts)


if __name__ == "__main__":
    main()
