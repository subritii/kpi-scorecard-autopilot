"""Database reads/writes for the endpoints. The math lives in kpis.py and drivers.py.

Every query filters by client_id explicitly (rule 3); RLS on the connection is
the backstop. Numbers come out of Postgres as Decimal and stay Decimal until
as_json() turns them into strings for the response.
"""

import dataclasses
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

import psycopg
from psycopg.types.json import Jsonb

from app import drivers, kpis

# share_of_movement is kept at full precision internally (and in drivers_json);
# API responses show it to 4 places.
SHARE_PLACES = Decimal("0.0001")


def drivers_output(result: drivers.DriverResult) -> dict:
    """DriverResult as JSON for API responses, with shares rounded to 4 places."""
    rounded = [
        dataclasses.replace(
            d,
            share_of_movement=None
            if d.share_of_movement is None
            else d.share_of_movement.quantize(SHARE_PLACES, rounding=ROUND_HALF_UP),
        )
        for d in result.drivers
    ]
    return as_json(dataclasses.replace(result, drivers=rounded))


def as_json(obj):
    """Decimals -> plain strings (never floats), dataclasses -> dicts, recursively."""
    if isinstance(obj, Decimal):
        return format(obj, "f")  # "131000.50", never "1.3100050E+5"
    if dataclasses.is_dataclass(obj):
        return {f.name: as_json(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {k: as_json(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [as_json(v) for v in obj]
    return obj


def week_data(conn: psycopg.Connection, client_id: str, week_start: date) -> drivers.WeekData:
    rows = conn.execute(
        "SELECT metric_key, detail_json, value FROM source_facts"
        " WHERE client_id = %s AND week_start = %s ORDER BY id",
        (client_id, week_start),
    ).fetchall()
    loaded = conn.execute(
        "SELECT source FROM source_loads WHERE client_id = %s AND week_start = %s",
        (client_id, week_start),
    ).fetchall()
    return drivers.WeekData(
        rows=tuple((r["metric_key"], r["detail_json"], r["value"]) for r in rows),
        loaded_sources=frozenset(r["source"] for r in loaded),
    )


def _value(formula_key: str, week: drivers.WeekData) -> Decimal | None:
    totals = kpis.metric_totals((m, v) for m, _, v in week.rows)
    return kpis.compute(formula_key, totals, week.loaded_sources)


def compute_week(conn: psycopg.Connection, client_id: str, week_start: date) -> list[dict] | None:
    """Compute every KPI for a client-week, upsert kpi_values, return the results.

    None if the client has no KPI definitions (unknown client, or not visible).
    """
    definitions = conn.execute(
        "SELECT * FROM kpi_definitions WHERE client_id = %s ORDER BY id", (client_id,)
    ).fetchall()
    if not definitions:
        return None

    current = week_data(conn, client_id, week_start)
    prior = week_data(conn, client_id, week_start - timedelta(weeks=1))

    results = []
    for d in definitions:
        key = d["formula_key"]
        value = _value(key, current)
        prior_value = _value(key, prior)
        status = kpis.status(value, d["target"], d["direction"], d["yellow_band_pct"])
        driver_result = drivers.drivers_for(key, current, prior)

        conn.execute(
            """
            INSERT INTO kpi_values
                (client_id, kpi_id, week_start, value, prior_value, status, drivers_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (client_id, kpi_id, week_start) DO UPDATE SET
                value = EXCLUDED.value,
                prior_value = EXCLUDED.prior_value,
                status = EXCLUDED.status,
                drivers_json = EXCLUDED.drivers_json
            """,
            (
                client_id,
                d["id"],
                week_start,
                value,
                prior_value,
                status,
                Jsonb(as_json(driver_result)),
            ),
        )
        results.append(
            {
                "kpi_id": d["id"],
                "name": d["name"],
                "formula_key": key,
                "value": value,
                "display_value": kpis.round_for_display(key, value),
                "prior_value": prior_value,
                "status": status,
                "target": d["target"],
                "direction": d["direction"],
                "yellow_band_pct": d["yellow_band_pct"],
            }
        )
    return results


def drivers_for_kpi(
    conn: psycopg.Connection, client_id: str, kpi_id: int, week_start: date, limit: int | None
) -> drivers.DriverResult | None:
    """Ranked drivers for one KPI-week. None if the KPI isn't this client's."""
    row = conn.execute(
        "SELECT formula_key FROM kpi_definitions WHERE id = %s AND client_id = %s",
        (kpi_id, client_id),
    ).fetchone()
    if row is None:
        return None
    return drivers.drivers_for(
        row["formula_key"],
        week_data(conn, client_id, week_start),
        week_data(conn, client_id, week_start - timedelta(weeks=1)),
        limit,
    )
