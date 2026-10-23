"""Load source reports that n8n pulled into source_facts and source_loads.

n8n fetches the report from QuickBooks with its own OAuth credential and posts
it here raw; parsing and every check happen in the API (app/qbo.py). Nothing
is written unless the whole report checks out.
"""

from datetime import date, timedelta
from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from app import qbo

AR_METRICS = ("ar_61_90", "ar_90_plus")


class ClientNotFound(LookupError):
    """No such client (or not visible: RLS) (HTTP 404)."""


class Forbidden(Exception):
    """This client can't receive this report (HTTP 403)."""


def ingest_ar_aging(
    conn: psycopg.Connection, client_id: str, week_start: date, realm_id: str, report: dict
) -> dict:
    """Write a client-week's AR over 60 buckets from an AgedReceivables report.

    The report must come from the client's own QuickBooks company (realm id),
    be dated within the week, and pass the parser's total cross-checks.
    Re-ingesting replaces the week's AR facts: customers missing from the new
    report are set to 0 (the app role can't delete rows), so totals stay right.
    """
    client = conn.execute("SELECT qbo_realm_id FROM clients WHERE id = %s", (client_id,)).fetchone()
    if client is None:
        raise ClientNotFound(f"client {client_id!r} not found")
    if client["qbo_realm_id"] is None:
        raise Forbidden(f"{client_id} has no QuickBooks company configured, so it takes no reports")
    if realm_id != client["qbo_realm_id"]:
        raise Forbidden("realm_id does not match this client's QuickBooks company")

    parsed = qbo.parse_aged_receivables(report)
    week_end = week_start + timedelta(days=6)
    if not week_start <= parsed.as_of <= week_end:
        raise qbo.ReportError(
            f"the report is dated {parsed.as_of}, outside the week of {week_start} to {week_end}"
        )

    # Sum per customer (and job), in case QuickBooks lists one twice.
    amounts: dict[tuple, list] = {}
    for row in parsed.rows:
        key = (row.customer, row.job, row.qbo_id)
        totals = amounts.setdefault(key, [Decimal(0), Decimal(0)])
        totals[0] += row.ar_61_90
        totals[1] += row.ar_91_plus

    # Replace the week's AR facts: zero everything, then write this report's values.
    conn.execute(
        "UPDATE source_facts SET value = 0"
        " WHERE client_id = %s AND week_start = %s AND metric_key = ANY(%s)",
        (client_id, week_start, list(AR_METRICS)),
    )
    facts = []
    for (customer, job, qbo_id), (a61, a91) in amounts.items():
        detail = {"customer": customer, "qbo_id": qbo_id} | ({"job": job} if job else {})
        facts += [
            (client_id, week_start, "ar_61_90", a61, Jsonb(detail)),
            (client_id, week_start, "ar_90_plus", a91, Jsonb(detail)),
        ]
    conn.cursor().executemany(
        "INSERT INTO source_facts (client_id, week_start, metric_key, value, detail_json)"
        " VALUES (%s, %s, %s, %s, %s)"
        " ON CONFLICT (client_id, week_start, metric_key, detail_json)"
        " DO UPDATE SET value = EXCLUDED.value",
        facts,
    )
    # Record that the AR aging report for this week was loaded (even if empty).
    conn.execute(
        "INSERT INTO source_loads (client_id, week_start, source, row_count)"
        " VALUES (%s, %s, 'qbo_ar_aging', %s)"
        " ON CONFLICT (client_id, week_start, source)"
        " DO UPDATE SET row_count = EXCLUDED.row_count, loaded_at = clock_timestamp()",
        (client_id, week_start, len(facts)),
    )
    return {
        "client_id": client_id,
        "week_start": week_start,
        "as_of": parsed.as_of,
        "rows": len(parsed.rows),
        "customers": len({customer for customer, _, _ in amounts}),
        "totals": {"ar_61_90": parsed.total_61_90, "ar_90_plus": parsed.total_91_plus},
    }
