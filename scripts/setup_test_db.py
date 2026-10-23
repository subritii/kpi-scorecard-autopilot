"""Create (if missing), migrate and seed the test database. Safe to re-run.

    python scripts/setup_test_db.py

The test suite runs against this database (TEST_DATABASE_URL and
TEST_ADMIN_DATABASE_URL in .env; see tests/conftest.py), never against the
dev/demo database, so rehearsing the demo can't break the tests and the tests
can't touch the demo data. Its name must end in "_test".

Roles (kpi_admin, app_user) are shared by every database on the Postgres
server, so the migrations' grants and row-level security apply here too.
"""

import os
from urllib.parse import urlsplit

import migrate  # same folder
import psycopg
import seed_db
from psycopg import sql

from app.config import load_env


def db_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def main() -> None:
    load_env()
    admin_url, app_url = os.environ["TEST_ADMIN_DATABASE_URL"], os.environ["TEST_DATABASE_URL"]
    name = db_name(admin_url)
    if not name.endswith("_test") or db_name(app_url) != name:
        raise SystemExit(
            f"refusing: test database must end in '_test' and both TEST_ URLs must name it "
            f"(got {name!r} and {db_name(app_url)!r})"
        )

    # CREATE DATABASE is a server-level command, so it runs from the dev admin connection.
    with psycopg.connect(os.environ["ADMIN_DATABASE_URL"], autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            print(f"created database {name}")

    # Point the normal scripts at the test database (environment beats .env).
    os.environ["ADMIN_DATABASE_URL"], os.environ["DATABASE_URL"] = admin_url, app_url
    migrate.main()
    seed_db.main()
    print(f"{name} is ready")


if __name__ == "__main__":
    main()
