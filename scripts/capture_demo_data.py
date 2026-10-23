"""Capture one example week from the real API code, for the landing page.

    python scripts/capture_demo_data.py      # writes data/demo/example_week.json

A dry run of the whole lifecycle for Brightline Services, week of Aug 3, 2026
(start, observations from the real local model, an owner reply, packet,
approval, send, ClickUp task), plus Northgate's all-green week and Sandbox Co's
QuickBooks data. It runs against the TEST database inside a transaction that is
always rolled back, so nothing is kept and the demo data is never touched.

What is saved is what the API returned, made safe to publish:
  - live email mode, so recipients are the fictional .example addresses (never
    test-mode Gmail aliases);
  - every signed link is replaced by a placeholder (no reply/approval tokens);
  - the file is refused if anything token-like, an alias, or a .env secret
    remains.
"""

import copy
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

import psycopg
from embed_docs import sync_docs  # same folder
from psycopg import sql
from psycopg.rows import dict_row

from app import ingest, links, packet, runs, service
from app.config import REPO_ROOT, load_env
from app.eval_cases import DOCS
from app.llm import get_provider

OUT = REPO_ROOT / "data" / "demo" / "example_week.json"
WEEK = date(2026, 8, 3)
SANDBOX_WEEK = date(2026, 10, 19)  # the saved real QuickBooks report is dated Oct 22, 2026
QBO_SAMPLE = REPO_ROOT / "data" / "samples" / "qbo_aged_receivables_real.json"
DEMO_REALM = "demo-realm"  # stands in for the real Company ID; never saved

# Avery's reply on the form (fictional, consistent with the planted event).
REPLY = {
    "what_changed": "Two customers moved past 60 days: Ambercrest Logistics and Larkspur Clinics.",
    "why": "Both switched to new AP systems in July and our invoices got stuck in "
    "their approval queues.",
    "owner_of_fix": "Avery Chen",
    "actions": "Called both AP teams this week; Ambercrest confirmed payment for Aug 14 "
    "and Larkspur for Aug 21. Weekly check-in until both clear.",
}
LINK = re.compile(r"(https?://\S+?/(?:approve|reply))\?token=[A-Za-z0-9_.\-]+")


def scrub(value):
    """Replace every signed link with a placeholder (recursively)."""
    if isinstance(value, str):
        return LINK.sub(r"\1?token=…", value)
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def env_file_values() -> dict[str, str]:
    """The values in .env itself (the capture changes some in os.environ)."""
    values = {}
    for line in (REPO_ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def assert_publishable(text: str) -> None:
    env = env_file_values()
    secrets = [
        env.get(k, "")
        for k in (
            "API_KEY",
            "LINK_SECRET",
            "POSTGRES_PASSWORD",
            "APP_DB_PASSWORD",
            "N8N_ENCRYPTION_KEY",
            "QBO_REALM_ID",
            "TEST_EMAIL_OVERRIDE",
        )
    ]
    problems = [
        *(["a .env secret"] if any(s and len(s) >= 6 and s in text for s in secrets) else []),
        *(["a link token"] if re.search(r"token=(?!…)[A-Za-z0-9_.\-]{8,}", text) else []),
        *(["a test-mode alias"] if re.search(r"\+[a-z0-9.\-]+@", text) else []),
        *(["a gmail address"] if "@gmail.com" in text else []),
        *(["the stand-in realm id"] if DEMO_REALM in text else []),
    ]
    if problems:
        raise SystemExit(f"refusing to save the demo data: it contains {', '.join(problems)}")


def kpi_rows(results: list[dict]) -> list[dict]:
    return [
        {
            "name": k["name"],
            "formula_key": k["formula_key"],
            "value": k["display_value"],
            "prior": packet.display(
                k["formula_key"], None if k["prior_value"] is None else Decimal(k["prior_value"])
            ),
            "target": packet.display(k["formula_key"], Decimal(k["target"])),
            "direction": k["direction"],
            "status": k["status"],
        }
        for k in results
    ]


def capture(conn, provider) -> dict:
    @contextmanager
    def connect(client_id):
        conn.execute(sql.SQL("SET LOCAL app.current_client_id = {}").format(sql.Literal(client_id)))
        yield conn

    sync_docs(conn, provider, DOCS)  # last year's explanations, for "a similar case"

    # --- Brightline: the full lifecycle --------------------------------------------------
    with connect("brightline") as c:
        run = runs.start(c, "brightline", WEEK)
        rid = run["run_id"]
        kpis_now = service.as_json(service.compute_week(c, "brightline", WEEK))
        ar = next(k for k in run["red_kpis"] if k["formula_key"] == "ar_over_60")
        drv = service.as_json(
            service.drivers_output(service.drivers_for_kpi(c, "brightline", ar["kpi_id"], WEEK, 3))
        )
    obs = runs.observations(connect, provider, "brightline", rid)
    [msg] = [m for m in obs["messages"] if m["kpi_id"] == ar["kpi_id"]]
    with connect("brightline") as c:
        runs.save_response(c, "brightline", rid, {"kpi_id": ar["kpi_id"], **REPLY})
        built = runs.build_packet(c, "brightline", rid)
        url = built["approval_email"]["body"]
        token = re.search(r"token=([A-Za-z0-9_.\-]+)", url).group(1)
        runs.decide_by_link(c, links.verify(token), "approved", "Looks good.")
        runs.claim_send(c, "brightline", rid, "demo-capture-0001")
        sent = service.as_json(runs.mark_sent(c, "brightline", rid, "demo-capture-0001"))
        approval = c.execute(
            "SELECT approver, decided_at FROM approvals"
            " WHERE run_id = %s AND decision = 'approved'",
            (rid,),
        ).fetchone()
        packet_email = runs.describe(c, "brightline", rid)["packet_email"]
        packet_html = c.execute(
            "SELECT packet_html FROM weekly_runs WHERE id = %s", (rid,)
        ).fetchone()["packet_html"]

    brightline = {
        "client_name": "Brightline Services",
        "week_label": packet.week_label(WEEK),
        "kpis": kpi_rows(kpis_now),
        "red_kpi": {
            "name": ar["name"],
            "owner_name": ar["owner_name"],
            "owner_email": ar["owner_email"],
        },
        "net_change": packet.display("ar_over_60", Decimal(drv["net_change"]), signed=True),
        "drivers": [
            {
                "name": d["name"],
                "change": packet.display("money", Decimal(d["change"]), signed=True),
                "share": packet.share_label(Decimal(d["share_of_movement"])),
            }
            for d in drv["drivers"]
        ],
        "owner_email": msg["email"],
        "observation": msg["observation"],
        "observation_source": msg["source"],
        "past_explanation_doc_key": msg["past_explanation_doc_key"],
        "reply": REPLY,
        "packet_html": packet_html,
        "packet_passed_check": built["faithfulness"]["passed"],
        "approval": {
            "approver": approval["approver"],
            "decided_at": approval["decided_at"].strftime("%Y-%m-%d %H:%M UTC"),
        },
        "packet_email": {"to": packet_email["to"], "subject": packet_email["subject"]},
        "clickup_task": sent["clickup_task"],
    }

    # --- Northgate: an all-green week ------------------------------------------------------
    with connect("northgate") as c:
        ng_run = runs.start(c, "northgate", WEEK)
        ng_kpis = service.as_json(service.compute_week(c, "northgate", WEEK))
    northgate = {
        "client_name": "Northgate Supply",
        "week_label": packet.week_label(WEEK),
        "kpis": kpi_rows(ng_kpis),
        "red_count": len(ng_run["red_kpis"]),
    }

    # --- Sandbox Co: the saved real QuickBooks sandbox report --------------------------------
    [report] = json.loads(QBO_SAMPLE.read_text())
    with connect("sandbox_co") as c:
        c.execute("UPDATE clients SET qbo_realm_id = %s WHERE id = 'sandbox_co'", (DEMO_REALM,))
        loaded = service.as_json(
            ingest.ingest_ar_aging(c, "sandbox_co", SANDBOX_WEEK, DEMO_REALM, copy.deepcopy(report))
        )
        sb_kpis = service.as_json(service.compute_week(c, "sandbox_co", SANDBOX_WEEK))
    sandbox = {
        "client_name": "Sandbox Co",
        "week_label": packet.week_label(SANDBOX_WEEK),
        "kpis": kpi_rows(sb_kpis),
        "as_of": loaded["as_of"],
        "customers": loaded["customers"],
        "totals": loaded["totals"],
    }
    return {
        "captured_on": date.today().isoformat(),
        "models": {
            "chat": getattr(provider, "chat_model", provider.name),
            "embeddings": provider.embedding_model,
        },
        "brightline": brightline,
        "northgate": northgate,
        "sandbox": sandbox,
    }


def main() -> int:
    load_env()
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not urlsplit(url).path.lstrip("/").endswith("_test"):
        print(
            "refusing: needs TEST_DATABASE_URL (*_test); run scripts/setup_test_db.py",
            file=sys.stderr,
        )
        return 2
    # Live mode for the capture only: show the fictional .example recipients, not aliases.
    os.environ["TEST_EMAIL_OVERRIDE"] = ""
    os.environ["EMAIL_MODE"] = "live"
    provider = get_provider()
    with psycopg.connect(url, row_factory=dict_row, autocommit=True) as conn:
        with conn.transaction(force_rollback=True):  # keep nothing
            data = scrub(capture(conn, provider))
    text = json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"  # dates as ISO
    assert_publishable(text)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text)
    source = data["brightline"]["observation_source"]
    print(f"wrote {OUT.relative_to(REPO_ROOT)} (observation source: {source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
