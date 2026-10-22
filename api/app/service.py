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

from app import drivers, faithfulness, kpis

# share_of_movement is kept at full precision internally (and in drivers_json);
# API responses show it to 4 places. Defined in faithfulness.py, which must accept
# numbers written from the shown value.
SHARE_PLACES = faithfulness.SHARE_PLACES


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


def kpi_exists(conn: psycopg.Connection, client_id: str, kpi_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM kpi_definitions WHERE id = %s AND client_id = %s", (kpi_id, client_id)
        ).fetchone()
        is not None
    )


# The packet lists each KPI's top drivers; "3 customers" in the text may refer
# to that list as well as to all contributors.
PACKET_DRIVERS_LISTED = 3


class NotComputed(Exception):
    """The run's week has no kpi_values yet (POST /compute first)."""


def _dec(value) -> Decimal | None:
    # drivers_json stores Decimals as strings (see as_json), never floats.
    return None if value is None else Decimal(value)


def _red_streak(statuses: list[tuple[date, str]], week_start: date) -> int:
    """Consecutive red weeks ending at week_start. A missing week breaks the streak."""
    by_week = dict(statuses)
    streak, week = 0, week_start
    while by_week.get(week) == "red":
        streak += 1
        week -= timedelta(weeks=1)
    return streak


def run_context(
    conn: psycopg.Connection, client_id: str, run_id: int
) -> faithfulness.RunContext | None:
    """Everything the faithfulness check may cite for one run. None if the run isn't
    this client's; NotComputed if its week has no KPI values yet."""
    run = conn.execute(
        "SELECT week_start FROM weekly_runs WHERE id = %s AND client_id = %s", (run_id, client_id)
    ).fetchone()
    if run is None:
        return None
    week_start = run["week_start"]

    client_name = conn.execute("SELECT name FROM clients WHERE id = %s", (client_id,)).fetchone()[
        "name"
    ]
    rows = conn.execute(
        """
        SELECT d.id, d.formula_key, d.name, d.direction, d.target, d.owner_name,
               v.value, v.prior_value, v.status, v.drivers_json
        FROM kpi_definitions d
        LEFT JOIN kpi_values v
               ON v.kpi_id = d.id AND v.client_id = d.client_id AND v.week_start = %s
        WHERE d.client_id = %s
        ORDER BY d.id
        """,
        (week_start, client_id),
    ).fetchall()
    if not any(r["status"] for r in rows):
        raise NotComputed(f"no KPI values for {client_id} week of {week_start}")

    history: dict[int, list[tuple[date, str]]] = {}
    for h in conn.execute(
        "SELECT kpi_id, week_start, status FROM kpi_values"
        " WHERE client_id = %s AND week_start <= %s",
        (client_id, week_start),
    ):
        history.setdefault(h["kpi_id"], []).append((h["week_start"], h["status"]))

    kpi_contexts = []
    for r in rows:
        stored = r["drivers_json"] or {}
        contributors = tuple(
            faithfulness.Contributor(
                name=c["name"],
                prior=Decimal(c["prior"]),
                current=Decimal(c["current"]),
                change=Decimal(c["change"]),
                share_of_movement=_dec(c["share_of_movement"]),
            )
            for c in stored.get("drivers", [])
        )
        den = stored.get("denominator")
        kpi_contexts.append(
            faithfulness.KpiContext(
                kpi_id=r["id"],
                formula_key=r["formula_key"],
                name=r["name"],
                direction=r["direction"],
                value=r["value"],
                prior_value=r["prior_value"],
                target=r["target"],
                net_change=_dec(stored.get("net_change")),
                contributors=contributors,
                denominator=faithfulness.Denominator(
                    den["metric"],
                    Decimal(den["prior"]),
                    Decimal(den["current"]),
                    Decimal(den["change"]),
                )
                if den
                else None,
                status=r["status"] or "no_data",
                red_streak=_red_streak(history.get(r["id"], []), week_start),
                listed_count=min(PACKET_DRIVERS_LISTED, len(contributors)),
            )
        )

    # owner_responses has no client_id; the join keeps the query client-scoped (rule 3).
    replies = conn.execute(
        """
        SELECT o.kpi_id, o.what_changed, o.why, o.owner_of_fix, o.actions
        FROM owner_responses o JOIN weekly_runs r ON r.id = o.run_id
        WHERE o.run_id = %s AND r.client_id = %s
        ORDER BY o.id
        """,
        (run_id, client_id),
    ).fetchall()
    return faithfulness.RunContext(
        week_start=week_start,
        client_name=client_name,
        people=tuple(sorted({r["owner_name"] for r in rows})),
        kpis=tuple(kpi_contexts),
        owner_responses=tuple(
            faithfulness.OwnerResponse(
                kpi_id=o["kpi_id"],
                what_changed=o["what_changed"] or "",
                why=o["why"] or "",
                owner_of_fix=o["owner_of_fix"] or "",
                actions=o["actions"] or "",
            )
            for o in replies
        ),
    )
