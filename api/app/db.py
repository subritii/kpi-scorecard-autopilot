"""Database access. Every connection is scoped to exactly one client.

Row-level security (db/migrations/003_rls.sql) only shows rows whose client_id
matches the session setting app.current_client_id. This module is the one
place that setting gets applied. Queries should still filter by client_id
explicitly; RLS is the backstop if one forgets.
"""

import os
from collections.abc import Generator
from contextlib import contextmanager

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


def database_url() -> str:
    # Connects as app_user (never the admin user), because superusers bypass RLS.
    return os.environ["DATABASE_URL"]


@contextmanager
def client_connection(client_id: str) -> Generator[psycopg.Connection, None, None]:
    """Yield a connection that can only see and write `client_id`'s rows.

    Everything inside the `with` block runs in one explicit transaction,
    committed on success and rolled back on error. SET LOCAL lasts only until
    that transaction ends, so the client id can never leak into a later
    request, even if the connection is reused.
    """
    if not client_id:
        raise ValueError("client_id is required")
    with psycopg.connect(database_url(), row_factory=dict_row, autocommit=True) as conn:
        with conn.transaction():
            # SET can't take a bind parameter, so the value is quoted with sql.Literal.
            conn.execute(
                sql.SQL("SET LOCAL app.current_client_id = {}").format(sql.Literal(client_id))
            )
            yield conn
