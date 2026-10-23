"""Evaluate the pipeline on the synthetic data's planted events; write eval/results.md.

    python scripts/run_eval.py

Needs the test database (python scripts/setup_test_db.py) and the real models
(Ollama). It never touches the dev/demo data:
  - KPI statuses, false alarms and drivers are computed from the seed files
    with the pure modules (no database);
  - retrieval, LLM observations, packets and the leak check run against the
    TEST database inside one transaction that is always rolled back.

Each metric is compared with its target from CLAUDE.md ("Evaluation targets").
"""

import csv
import json
import os
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

import psycopg
from embed_docs import sync_docs  # same folder
from psycopg import sql
from psycopg.rows import dict_row

from app import drivers, faithfulness, kpis, retrieval, runs, service
from app.config import REPO_ROOT, load_env
from app.drivers import WeekData
from app.eval_cases import DOCS, NO_HISTORY_QUERY, planted_cases
from app.llm import get_provider

SEED = REPO_ROOT / "data" / "seed"
RESULTS = REPO_ROOT / "eval" / "results.md"
CLIENTS = ("brightline", "northgate")
# Every client-owned table, and how to find the owning client of a visible row.
OWNER_SQL = {
    "clients": "SELECT id AS owner FROM clients",
    "kpi_definitions": "SELECT client_id AS owner FROM kpi_definitions",
    "source_facts": "SELECT client_id AS owner FROM source_facts",
    "source_loads": "SELECT client_id AS owner FROM source_loads",
    "kpi_values": "SELECT client_id AS owner FROM kpi_values",
    "weekly_runs": "SELECT client_id AS owner FROM weekly_runs",
    "doc_chunks": "SELECT client_id AS owner FROM doc_chunks",
    "owner_responses": "SELECT r.client_id AS owner FROM owner_responses o"
    " LEFT JOIN weekly_runs r ON r.id = o.run_id",
    "approvals": "SELECT r.client_id AS owner FROM approvals a"
    " LEFT JOIN weekly_runs r ON r.id = a.run_id",
    "run_events": "SELECT r.client_id AS owner FROM run_events e"
    " LEFT JOIN weekly_runs r ON r.id = e.run_id",
    "link_tokens": "SELECT r.client_id AS owner FROM link_tokens t"
    " LEFT JOIN weekly_runs r ON r.id = t.run_id",
    "claim_resolutions": "SELECT r.client_id AS owner FROM claim_resolutions c"
    " LEFT JOIN weekly_runs r ON r.id = c.run_id",
}


# --- seed data -----------------------------------------------------------------------


def load_seed():
    definitions = json.loads((SEED / "kpi_definitions.json").read_text())
    ground_truth = json.loads((SEED / "ground_truth.json").read_text())
    rows, loaded = defaultdict(list), defaultdict(set)
    with open(SEED / "source_facts.csv", newline="") as f:
        for r in csv.DictReader(f):
            rows[(r["client_id"], r["week_start"])].append(
                (r["metric_key"], json.loads(r["detail_json"]), Decimal(r["value"]))
            )
    with open(SEED / "source_loads.csv", newline="") as f:
        for r in csv.DictReader(f):
            loaded[(r["client_id"], r["week_start"])].add(r["source"])
    weeks = {k: WeekData(tuple(rows[k]), frozenset(loaded[k])) for k in loaded}
    return definitions, ground_truth, weeks


def hand_value(formula_key: str, week: WeekData) -> Decimal | None:
    """An independent, written-out version of each formula, to check kpis.py against."""

    def total(metric):
        return sum((v for m, _, v in week.rows if m == metric), Decimal(0))

    if formula_key == "weekly_revenue":
        return total("revenue")
    if formula_key == "gross_margin_pct":
        revenue = total("revenue")
        return None if revenue == 0 else (revenue - total("cogs")) / revenue * 100
    if formula_key == "cash_on_hand":
        return total("cash_ending")
    if formula_key == "ar_over_60":
        return total("ar_61_90") + total("ar_90_plus")
    if formula_key == "new_bookings":
        return total("bookings")
    if formula_key == "inventory_days":
        cogs = total("cogs")
        return None if cogs == 0 else total("inventory_balance") / (cogs / 7)
    raise KeyError(formula_key)


# --- 1. KPI math, statuses, false alarms, drivers (pure) ---------------------------------


def eval_kpis(definitions, ground_truth, weeks) -> dict:
    by_kpi = {(d["client_id"], d["formula_key"]): d for d in definitions}
    math_ok = status_ok = 0
    false_alarms, misses = [], []
    for e in ground_truth["expected_statuses"]:
        d = by_kpi[(e["client_id"], e["formula_key"])]
        week = weeks[(e["client_id"], e["week_start"])]
        totals = kpis.metric_totals((m, v) for m, _, v in week.rows)
        value = kpis.compute(e["formula_key"], totals, week.loaded_sources)
        hand = hand_value(e["formula_key"], week)
        if (value is None and hand is None) or abs(value - hand) < Decimal("1e-9"):
            math_ok += 1
        got = kpis.status(
            value, Decimal(str(d["target"])), d["direction"], Decimal(str(d["yellow_band_pct"]))
        )
        if got == e["expected_status"]:
            status_ok += 1
        elif e["expected_status"] == "green":
            false_alarms.append(e)
        else:
            misses.append(e)

    events_detected = 0
    for event in ground_truth["events"]:
        planted = {
            (event["client_id"], event["formula_key"], w["week_start"]) for w in event["weeks"]
        }
        if not any((m["client_id"], m["formula_key"], m["week_start"]) in planted for m in misses):
            events_detected += 1

    week_list = ground_truth["week_starts"]
    driver_hits = driver_total = 0
    for event in ground_truth["events"]:
        for w in event["weeks"]:
            prior = week_list[week_list.index(w["week_start"]) - 1]
            result = drivers.drivers_for(
                event["formula_key"],
                weeks[(event["client_id"], w["week_start"])],
                weeks[(event["client_id"], prior)],
            )
            driver_total += 1
            driver_hits += result.drivers[0].name in event["top_drivers"]

    n = len(ground_truth["expected_statuses"])
    return {
        "values": n,
        "client_weeks": len(
            {(e["client_id"], e["week_start"]) for e in ground_truth["expected_statuses"]}
        ),
        "math_ok": math_ok,
        "status_ok": status_ok,
        "false_alarms": false_alarms,
        "misses": misses,
        "events": len(ground_truth["events"]),
        "events_detected": events_detected,
        "driver_hits": driver_hits,
        "driver_total": driver_total,
    }


# --- 2-4. Retrieval, real LLM text, leaks (test database, rolled back) ---------------------


def scoped(conn, client_id):
    conn.execute(sql.SQL("SET LOCAL app.current_client_id = {}").format(sql.Literal(client_id)))


def eval_with_models(conn, provider, ground_truth) -> dict:
    out: dict = {}
    sync_docs(conn, provider, DOCS)
    threshold = retrieval.min_similarity()
    cases = planted_cases()
    queries = [c.query for c in cases] + [NO_HISTORY_QUERY]
    vectors = dict(zip(queries, provider.embed(queries, purpose="query"), strict=True))

    # Retrieval, exactly as /retrieve does it: this client's KPI, before the week, threshold.
    relevant, correct_scores, wrong_scores = 0, [], []
    for c in cases:
        scoped(conn, c.client_id)
        kpi_id = conn.execute(
            "SELECT id FROM kpi_definitions WHERE client_id = %s AND formula_key = %s",
            (c.client_id, c.formula_key),
        ).fetchone()["id"]
        found = retrieval.search(
            conn,
            c.client_id,
            vectors[c.query],
            before=c.week_start,
            kpi_id=kpi_id,
            limit=3,
            min_similarity=threshold,
        )
        relevant += bool(found.results) and found.results[0].doc_key == c.expected_doc_key
        # All of the client's past explanations, for the score gap.
        for m in retrieval.search(
            conn, c.client_id, vectors[c.query], before=c.week_start, kpi_id=None, limit=50
        ).results:
            (correct_scores if m.doc_key == c.expected_doc_key else wrong_scores).append(
                m.similarity
            )
    no_history_returned = 0
    for client_id in CLIENTS:
        scoped(conn, client_id)
        found = retrieval.search(
            conn,
            client_id,
            vectors[NO_HISTORY_QUERY],
            before="2026-07-06",
            kpi_id=None,
            limit=50,
            min_similarity=threshold,
        )
        no_history_returned += len(found.results)
        wrong_scores += [
            m.similarity
            for m in retrieval.search(
                conn,
                client_id,
                vectors[NO_HISTORY_QUERY],
                before="2026-07-06",
                kpi_id=None,
                limit=50,
            ).results
        ]
    out["retrieval"] = {
        "cases": len(cases),
        "relevant": relevant,
        "threshold": threshold,
        "lowest_correct": min(correct_scores),
        "highest_wrong": max(wrong_scores),
        "no_history_returned": no_history_returned,
    }

    # Real LLM observations and packets, for every red planted week.
    @contextmanager
    def connect(client_id):
        scoped(conn, client_id)
        yield conn

    red_weeks = sorted(
        {
            (e["client_id"], e["week_start"])
            for e in ground_truth["expected_statuses"]
            if e["expected_status"] == "red"
        }
    )
    sources, first_try, final_pass, packets_pass, seconds = Counter(), 0, 0, 0, []
    run_ids = {}
    for client_id, week_start in red_weeks:
        with connect(client_id) as c:
            run = runs.start(c, client_id, date.fromisoformat(week_start))
        run_ids[(client_id, week_start)] = run["run_id"]
        started = time.time()
        messages = runs.observations(connect, provider, client_id, run["run_id"])["messages"]
        seconds.append(time.time() - started)
        with connect(client_id) as c:
            ctx = service.run_context(c, client_id, run["run_id"])
        for m in messages:
            sources[m["source"]] += 1
            first_try += m["source"] == "llm"
            final_pass += faithfulness.check(m["observation"], ctx).passed
        with connect(client_id) as c:
            built = runs.build_packet(c, client_id, run["run_id"])
        packets_pass += built["faithfulness"]["passed"]
    out["llm"] = {
        "model": getattr(provider, "chat_model", provider.name),
        "observations": sum(sources.values()),
        "first_try": first_try,
        "sources": dict(sources),
        "final_pass": final_pass,
        "packets": len(red_weeks),
        "packets_pass": packets_pass,
        "seconds_per_run": sum(seconds) / len(seconds),
    }

    # Cross-client leaks: as each client, count visible rows owned by anyone else,
    # in every table (the runs, links, events and packets above included).
    leaks = {}
    for client_id in CLIENTS:
        scoped(conn, client_id)
        for table, query in OWNER_SQL.items():
            owners = [r["owner"] for r in conn.execute(query)]
            leaks[(client_id, table)] = sum(o != client_id for o in owners)
    # And no packet names another client's contributors.
    names = defaultdict(set)
    for (client_id, _), run_id in run_ids.items():
        scoped(conn, client_id)
        ctx = service.run_context(conn, client_id, run_id)
        names[client_id] |= {c.name for k in ctx.kpis for c in k.contributors}
    packet_leaks = 0
    for (client_id, _), run_id in run_ids.items():
        scoped(conn, client_id)
        md = conn.execute("SELECT packet_md FROM weekly_runs WHERE id = %s", (run_id,)).fetchone()[
            "packet_md"
        ]
        other = names[next(c for c in CLIENTS if c != client_id)] - names[client_id]
        packet_leaks += sum(name in md for name in other)
    out["leaks"] = {
        "rows": sum(leaks.values()),
        "tables": len(OWNER_SQL),
        "packet_names": packet_leaks,
    }
    return out


# --- report ----------------------------------------------------------------------------------


def write_report(k: dict, m: dict, provider) -> str:
    r, llm, leaks = m["retrieval"], m["llm"], m["leaks"]
    pct = lambda a, b: f"{100 * a / b:.0f}%"  # noqa: E731
    rows = [
        (
            "KPI math accuracy vs. hand-computed",
            "100%",
            f"{pct(k['math_ok'], k['values'])} ({k['math_ok']}/{k['values']})",
            k["math_ok"] == k["values"],
        ),
        (
            "KPI status vs. ground truth",
            "100%",
            f"{pct(k['status_ok'], k['values'])} ({k['status_ok']}/{k['values']})",
            k["status_ok"] == k["values"],
        ),
        (
            "Planted events detected",
            "all",
            f"{k['events_detected']}/{k['events']}",
            k["events_detected"] == k["events"],
        ),
        (
            f"False alarms across {k['client_weeks']} client-weeks",
            "≤ 1",
            str(len(k["false_alarms"])),
            len(k["false_alarms"]) <= 1,
        ),
        (
            "Top driver = planted cause",
            "≥ 90%",
            f"{pct(k['driver_hits'], k['driver_total'])} "
            f"({k['driver_hits']}/{k['driver_total']} event weeks)",
            k["driver_hits"] / k["driver_total"] >= 0.9,
        ),
        (
            "Narrative numbers match data (after check)",
            "100%",
            f"{pct(llm['final_pass'], llm['observations'])} "
            f"({llm['final_pass']}/{llm['observations']} observations)",
            llm["final_pass"] == llm["observations"],
        ),
        (
            "Packets pass the faithfulness check",
            "100%",
            f"{llm['packets_pass']}/{llm['packets']}",
            llm["packets_pass"] == llm["packets"],
        ),
        (
            "Retrieved explanation relevant",
            "≥ 80%",
            f"{pct(r['relevant'], r['cases'])} ({r['relevant']}/{r['cases']} planted events)",
            r["relevant"] / r["cases"] >= 0.8,
        ),
        (
            "Query with no history returns nothing",
            "0 results",
            str(r["no_history_returned"]),
            r["no_history_returned"] == 0,
        ),
        (
            "Cross-client leaks",
            "0",
            f"{leaks['rows']} rows in {leaks['tables']} tables; "
            f"{leaks['packet_names']} names in packets",
            leaks["rows"] == 0 and leaks["packet_names"] == 0,
        ),
    ]
    table = "\n".join(
        f"| {name} | {target} | {result} | {'pass' if ok else '**FAIL**'} |"
        for name, target, result, ok in rows
    )
    sources = ", ".join(f"{n} {s}" for s, n in sorted(llm["sources"].items()))
    report = f"""# Evaluation results

Run on {date.today():%b} {date.today().day}, {date.today().year} with `python scripts/run_eval.py`.
Synthetic data: 2 clients x 12 weeks ({k["values"]} client-week-KPI values), with the
planted events in `data/seed/ground_truth.json`. Models: chat `{llm["model"]}`,
embeddings `{provider.embedding_model}` (Ollama, local).

| Metric | Target | Result | |
|---|---|---|---|
{table}

## Details

- **KPI math** is checked against a second, written-out implementation of each
  formula in the eval script (exact Decimal arithmetic, compared to 1e-9).
- **False alarms** are non-green statuses where the ground truth says green;
  **misses** (planted weeks not flagged): {len(k["misses"])}.
- **Drivers**: the first-ranked contributor is a planted cause in
  {k["driver_hits"]} of {k["driver_total"]} event weeks (spikes and wind-downs).
- **LLM observations** (`{llm["model"]}`, temperature 0): {llm["first_try"]} of
  {llm["observations"]} passed the faithfulness check on the first try. Final
  sources: {sources}. A failing draft gets one retry, then falls back to a
  template built only from computed numbers, so what is sent always passes.
  Timing ({llm["seconds_per_run"]:.1f}s per run here) depends heavily on the
  machine and on what else is running; it is not a benchmark.
- **Packets** are built without owner replies (nobody answers during an
  automated run), so they exercise the "No reply yet" path; packets with
  verbatim owner quotes are covered by the unit tests.
- **Retrieval**: threshold `RETRIEVAL_MIN_SIMILARITY={r["threshold"]}`. Lowest
  correct match {r["lowest_correct"]:.4f}, highest wrong match
  {r["highest_wrong"]:.4f} (gap {r["lowest_correct"] - r["highest_wrong"]:.4f}).
  Tuned on this small synthetic set; see the README.
- **Leaks**: as each client, every visible row in all {leaks["tables"]}
  client-owned tables belongs to that client (row-level security), and no
  packet names another client's customers or accounts.
"""
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(report)
    return report


def main() -> int:
    load_env()
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not urlsplit(url).path.lstrip("/").endswith("_test"):
        print(
            "refusing: set TEST_DATABASE_URL to the *_test database (scripts/setup_test_db.py)",
            file=sys.stderr,
        )
        return 2
    definitions, ground_truth, weeks = load_seed()
    k = eval_kpis(definitions, ground_truth, weeks)
    provider = get_provider()
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        with conn.transaction(force_rollback=True):  # leave the test database as it was
            m = eval_with_models(conn, provider, ground_truth)
    print(write_report(k, m, provider))
    return 0


if __name__ == "__main__":
    sys.exit(main())
