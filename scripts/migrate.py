"""Apply db/migrations/*.sql in filename order, each exactly once.

Runs as the admin user (ADMIN_DATABASE_URL). Applied files are recorded in
schema_migrations, so re-running only applies new files. Afterwards it makes
sure the kpi_app role can log in with APP_DB_PASSWORD.
"""

import os

import psycopg
from psycopg import sql

from app.config import REPO_ROOT, load_env

MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"


def render(text: str) -> str:
    """Fill in {{EMBEDDING_DIM}} so the vector size comes from .env, not the SQL."""
    dim = os.environ["EMBEDDING_DIM"]
    if not dim.isdigit():
        raise ValueError(f"EMBEDDING_DIM must be a positive integer, got {dim!r}")
    return text.replace("{{EMBEDDING_DIM}}", dim)


def main() -> None:
    load_env()
    # autocommit so each `with conn.transaction()` below is its own real transaction.
    with psycopg.connect(os.environ["ADMIN_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            print(f"applying {path.name}")
            # A failed file rolls back entirely and is not recorded.
            with conn.transaction():
                conn.execute(render(path.read_text()))
                conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))

        # sql.Literal quotes the password safely; it is never printed.
        conn.execute(
            sql.SQL("ALTER ROLE kpi_app WITH LOGIN PASSWORD {}").format(
                sql.Literal(os.environ["APP_DB_PASSWORD"])
            )
        )
    print("migrations up to date")


if __name__ == "__main__":
    main()
