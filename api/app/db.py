"""Database access. Every connection is scoped to exactly one client.

Row-level security (db/migrations/003_rls.sql) only shows rows whose client_id
matches the session setting app.current_client_id. This module is the one
place that setting gets applied. Queries should still filter by client_id
explicitly; RLS is the backstop if one forgets.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row


def database_url() -> str:
    # Connects as kpi_app (not the admin user), because superusers bypass RLS.
    return os.environ["DATABASE_URL"]


@contextmanager
def client_connection(client_id: str) -> Iterator[psycopg.Connection]:
    """Yield a connection that can only see and write `client_id`'s rows.

    Everything inside the `with` block runs in one transaction, committed on
    success and rolled back on error. set_config(..., true) makes the setting
    transaction-local, so it vanishes at commit and can never carry over to
    another client. Don't commit mid-block: after a commit the setting is
    gone and queries return nothing.
    """
    if not client_id:
        raise ValueError("client_id is required")
    with psycopg.connect(database_url(), row_factory=dict_row) as conn:
        conn.execute("SELECT set_config('app.current_client_id', %s, true)", (client_id,))
        yield conn
