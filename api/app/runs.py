"""Weekly run lifecycle: the state machine and every operation n8n triggers.

    collecting -> asking -> assembling -> awaiting_approval -> sent
                                     ^             |
                                     +- rejected <-+
    any open state -> needs_attention -> asking | assembling

Every state change goes through move(): it checks the transition is allowed,
locks the run row so two requests can't race, and appends to run_events.
Nothing is sent without an approval of the current packet version (checked
here and by a database trigger, rule 4).
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date

import psycopg

from app import email, faithfulness, packet, prompts, retrieval, service
from app.faithfulness import OwnerResponse
from app.llm.base import LLMError, LLMProvider

TRANSITIONS: dict[str, frozenset[str]] = {
    "collecting": frozenset({"asking", "needs_attention"}),
    "asking": frozenset({"assembling", "needs_attention"}),
    "assembling": frozenset({"awaiting_approval", "needs_attention"}),
    "awaiting_approval": frozenset({"sent", "rejected", "needs_attention"}),
    "rejected": frozenset({"assembling", "needs_attention"}),
    "needs_attention": frozenset({"asking", "assembling"}),
    "sent": frozenset(),  # final
}

# States in which each step may run.
OBSERVE_STATES = {"asking", "needs_attention"}
REPLY_STATES = {"asking", "needs_attention", "rejected"}
PACKET_STATES = {"asking", "rejected", "needs_attention"}


class RunNotFound(LookupError):
    """No such run for this client (or another client's run: RLS hides it)."""


class Conflict(Exception):
    """The run's state doesn't allow this step (HTTP 409)."""


class InvalidTransition(Conflict):
    pass


class Forbidden(Exception):
    """The caller isn't allowed to do this (HTTP 403)."""


Connect = Callable[[str], AbstractContextManager[psycopg.Connection]]


def validate_transition(current: str, new: str) -> None:
    if new not in TRANSITIONS[current]:
        raise InvalidTransition(f"run is {current}; {current} -> {new} is not allowed")


def _require_state(run: dict, allowed: set[str], step: str) -> None:
    if run["state"] not in allowed:
        raise Conflict(f"cannot {step} while the run is {run['state']}")


# --- database helpers --------------------------------------------------------------


def get_run(conn: psycopg.Connection, client_id: str, run_id: int, lock: bool = False) -> dict:
    """The run, optionally locked (FOR UPDATE) until the transaction ends."""
    row = conn.execute(
        "SELECT id, client_id, week_start, state, packet_md, packet_version FROM weekly_runs"
        " WHERE id = %s AND client_id = %s" + (" FOR UPDATE" if lock else ""),
        (run_id, client_id),
    ).fetchone()
    if row is None:
        raise RunNotFound(f"run {run_id} not found for {client_id}")
    return row


def move(conn: psycopg.Connection, run: dict, new_state: str, note: str | None = None) -> None:
    """Change state (if allowed) and log it. The run must be locked by the caller."""
    validate_transition(run["state"], new_state)
    conn.execute(
        "UPDATE weekly_runs SET state = %s WHERE id = %s AND client_id = %s",
        (new_state, run["id"], run["client_id"]),
    )
    conn.execute(
        "INSERT INTO run_events (run_id, from_state, to_state, note) VALUES (%s, %s, %s, %s)",
        (run["id"], run["state"], new_state, note),
    )
    run["state"] = new_state


def _kpi_owners(conn: psycopg.Connection, client_id: str) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT id, name, formula_key, owner_name, owner_email FROM kpi_definitions"
        " WHERE client_id = %s",
        (client_id,),
    )
    return {r["id"]: r for r in rows}


def _red_kpis(conn: psycopg.Connection, client_id: str, week_start: date) -> list[dict]:
    rows = conn.execute(
        """
        SELECT d.id, d.name, d.formula_key, d.owner_name, d.owner_email, v.value
        FROM kpi_values v
        JOIN kpi_definitions d ON d.id = v.kpi_id AND d.client_id = v.client_id
        WHERE v.client_id = %s AND v.week_start = %s AND v.status = 'red'
        ORDER BY d.id
        """,
        (client_id, week_start),
    ).fetchall()
    return [
        {
            "kpi_id": r["id"],
            "name": r["name"],
            "formula_key": r["formula_key"],
            "value": None if r["value"] is None else format(r["value"], "f"),
            "display_value": packet.display(r["formula_key"], r["value"]),
            "owner_name": r["owner_name"],
            "owner_email": email.recipient(r["owner_email"]),
        }
        for r in rows
    ]


def _answered(conn: psycopg.Connection, client_id: str, run_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT DISTINCT o.kpi_id FROM owner_responses o JOIN weekly_runs r ON r.id = o.run_id"
        " WHERE o.run_id = %s AND r.client_id = %s",
        (run_id, client_id),
    )
    return {r["kpi_id"] for r in rows}


def _approved(conn: psycopg.Connection, run: dict) -> bool:
    """Approved for the current packet version (older approvals don't count)."""
    return (
        conn.execute(
            "SELECT 1 FROM approvals a JOIN weekly_runs r ON r.id = a.run_id"
            " WHERE a.run_id = %s AND r.client_id = %s AND a.decision = 'approved'"
            " AND a.packet_version = %s",
            (run["id"], run["client_id"], run["packet_version"]),
        ).fetchone()
        is not None
    )


# --- steps -----------------------------------------------------------------------------


def start(conn: psycopg.Connection, client_id: str, week_start: date) -> dict:
    """Create (or find) the week's run, compute it, and move collecting -> asking.

    Safe to call twice: an existing run is returned as it is, and a run that has
    moved past collecting is never recomputed or reset.
    """
    inserted = conn.execute(
        "INSERT INTO weekly_runs (client_id, week_start) VALUES (%s, %s)"
        " ON CONFLICT (client_id, week_start) DO NOTHING RETURNING id",
        (client_id, week_start),
    ).fetchone()
    if inserted:
        conn.execute(
            "INSERT INTO run_events (run_id, from_state, to_state) VALUES (%s, NULL, 'collecting')",
            (inserted["id"],),
        )
    run_id = (
        inserted["id"]
        if inserted
        else conn.execute(
            "SELECT id FROM weekly_runs WHERE client_id = %s AND week_start = %s",
            (client_id, week_start),
        ).fetchone()["id"]
    )
    run = get_run(conn, client_id, run_id, lock=True)
    if run["state"] == "collecting":
        if service.compute_week(conn, client_id, week_start) is None:
            raise RunNotFound(f"no KPI definitions for {client_id}")
        move(conn, run, "asking")
    return {
        "run_id": run["id"],
        "client_id": client_id,
        "week_start": week_start,
        "state": run["state"],
        "created": inserted is not None,
        "red_kpis": _red_kpis(conn, client_id, week_start),
    }


def describe(conn: psycopg.Connection, client_id: str, run_id: int) -> dict:
    """Read-only view for n8n: state, packet, recipients, approval, unanswered KPIs."""
    run = get_run(conn, client_id, run_id)
    c = conn.execute(
        "SELECT name, controller_email, leadership_emails, clickup_list_id FROM clients"
        " WHERE id = %s",
        (client_id,),
    ).fetchone()
    answered = _answered(conn, client_id, run_id)
    red = _red_kpis(conn, client_id, run["week_start"])
    owners = _kpi_owners(conn, client_id)
    week = packet.week_label(run["week_start"])
    approved = _approved(conn, run)
    # The packet email exists only once the current version is approved (rule 4).
    packet_email = (
        email.compose(
            c["leadership_emails"], f"KPI packet: {c['name']}, week of {week}", run["packet_md"]
        )
        if approved and run["packet_md"]
        else None
    )
    reminder_emails = [
        email.compose(
            [owners[k["kpi_id"]]["owner_email"]],
            f"Reminder: {k['name']} for the week of {week} needs your input",
            f"Hi {k['owner_name'].split()[0]},\n\nA reminder to answer the request about "
            f"{k['name']} for the week of {week} by Tuesday 12:00.",
        )
        for k in red
        if k["kpi_id"] not in answered
    ]
    return {
        "run_id": run["id"],
        "client_id": client_id,
        "week_start": run["week_start"],
        "state": run["state"],
        "packet_md": run["packet_md"],
        "packet_version": run["packet_version"],
        "approved": approved,
        "controller_email": email.recipient(c["controller_email"]),
        "leadership_emails": email.recipients(c["leadership_emails"]),
        "clickup_list_id": c["clickup_list_id"],
        "red_kpis": [{**k, "answered": k["kpi_id"] in answered} for k in red],
        "unanswered_kpis": [k for k in red if k["kpi_id"] not in answered],
        "packet_email": packet_email,
        "reminder_emails": reminder_emails,
    }


def _facts(k: faithfulness.KpiContext, week_start: date) -> dict[str, str]:
    """The prompt's values, already formatted: the LLM only copies them."""
    movers = packet.movers(k)
    return {
        "week": packet.week_label(week_start),
        "kpi_name": k.name,
        "direction_note": "higher is better" if k.direction == "higher" else "lower is better",
        "status": k.status,
        "value": packet.display(k.formula_key, k.value),
        "prior": packet.display(k.formula_key, k.prior_value),
        "change": packet.display(k.formula_key, k.net_change, signed=True)
        if k.net_change is not None
        else "no data",
        "target": packet.display(k.formula_key, k.target),
        "movers": "\n".join(
            f"- {c.name}: {packet.display('money', c.change, signed=True)}"
            + (f", {packet.share_label(c.share_of_movement)}" if c.share_of_movement else "")
            for c in movers
        )
        or "- none",
    }


def _problems_json(result: faithfulness.CheckResult) -> list[dict]:
    return [{"type": p.type, "text": p.text, "reason": p.reason} for p in result.problems]


def observations(connect: Connect, llm: LLMProvider, client_id: str, run_id: int) -> dict:
    """An owner message for each red KPI.

    The LLM writes only the observation (what moved and which contributors),
    from pre-formatted numbers, and every draft is faithfulness-checked. A
    failing draft gets one retry with the problems listed; if that fails too,
    or the LLM is unreachable, the observation comes from the template, built
    only from computed numbers. Last year's explanation (if one clears the
    retrieval threshold) is quoted verbatim in the message, never sent to the LLM.
    """
    threshold = retrieval.min_similarity()
    with connect(client_id) as conn:
        run = get_run(conn, client_id, run_id)
        _require_state(run, OBSERVE_STATES, "write observations")
        ctx = service.run_context(conn, client_id, run_id)
        owners = _kpi_owners(conn, client_id)
    system = prompts.render("owner_observation_system.md")

    messages = []
    for k in (k for k in ctx.kpis if k.status == "red"):
        # Model calls happen outside any database transaction.
        past = None
        query = f"{k.name} moved this week. Biggest movers: " + (
            ", ".join(c.name for c in packet.movers(k)) or "none"
        )
        try:
            [vector] = llm.embed([query], purpose="query")
            with connect(client_id) as conn:
                found = retrieval.search(
                    conn,
                    client_id,
                    vector,
                    before=ctx.week_start,
                    kpi_id=k.kpi_id,
                    limit=1,
                    min_similarity=threshold,
                )
            past = found.results[0] if found.results else None
        except LLMError:
            past = None  # no history is better than no message

        prompt = prompts.render("owner_observation.md", **_facts(k, ctx.week_start))
        observation, source, attempts, first_problems, llm_error = None, "template", 0, [], None
        try:
            for attempt in (1, 2):
                attempts = attempt
                draft = llm.complete(prompt, system=system).strip()
                result = faithfulness.check(draft, ctx)
                if result.passed:
                    observation, source = draft, "llm" if attempt == 1 else "llm_retry"
                    break
                if attempt == 1:
                    first_problems = _problems_json(result)
                    prompt = prompts.render(
                        "owner_observation_retry.md",
                        original_prompt=prompt,
                        draft=draft,
                        problems="\n".join(
                            f"- {p['text']!r}: {p['reason']}" for p in first_problems
                        ),
                    )
        except LLMError as e:
            llm_error = str(e)

        template_check_passed = None
        if observation is None:
            observation = packet.observation_template(k, ctx.week_start)
            template_check_passed = faithfulness.check(observation, ctx).passed

        owner = owners[k.kpi_id]
        message = packet.owner_message(
            owner["owner_name"],
            observation,
            (packet.week_label(past.week_start), past.text) if past else None,
        )
        messages.append(
            {
                "kpi_id": k.kpi_id,
                "kpi_name": k.name,
                "formula_key": k.formula_key,
                "owner_name": owner["owner_name"],
                "owner_email": email.recipient(owner["owner_email"]),
                "observation": observation,
                "message": message,
                "email": email.compose(
                    [owner["owner_email"]],
                    f"Week of {packet.week_label(ctx.week_start)}: {k.name} needs your input",
                    message,
                ),
                "source": source,
                "attempts": attempts,
                "first_attempt_problems": first_problems,
                "template_check_passed": template_check_passed,
                "llm_error": llm_error,
                "past_explanation_doc_key": past.doc_key if past else None,
            }
        )
    return {"run_id": run_id, "client_id": client_id, "messages": messages}


def save_response(conn: psycopg.Connection, client_id: str, run_id: int, reply: dict) -> dict:
    run = get_run(conn, client_id, run_id, lock=True)
    _require_state(run, REPLY_STATES, "save a reply")
    if not service.kpi_exists(conn, client_id, reply["kpi_id"]):
        raise RunNotFound(f"KPI {reply['kpi_id']} not found for {client_id}")
    row = conn.execute(
        "INSERT INTO owner_responses (run_id, kpi_id, what_changed, why, owner_of_fix, actions)"
        " VALUES (%(run_id)s, %(kpi_id)s, %(what_changed)s, %(why)s, %(owner_of_fix)s,"
        " %(actions)s) RETURNING id",
        {**reply, "run_id": run_id},
    ).fetchone()
    return {"response_id": row["id"], "run_id": run_id, "kpi_id": reply["kpi_id"]}


def build_packet(conn: psycopg.Connection, client_id: str, run_id: int) -> dict:
    """Assemble the packet, check it, and move to awaiting_approval.

    Each build bumps packet_version, so approvals of an earlier version no
    longer count. A packet that fails the check goes to needs_attention.
    """
    run = get_run(conn, client_id, run_id, lock=True)
    _require_state(run, PACKET_STATES, "build the packet")
    move(conn, run, "assembling")

    ctx = service.run_context(conn, client_id, run_id)
    owners = {
        i: (o["owner_name"], o["owner_email"]) for i, o in _kpi_owners(conn, client_id).items()
    }
    latest: dict[int, OwnerResponse] = {}
    for r in ctx.owner_responses:  # ordered by id, so the last one per KPI wins
        latest[r.kpi_id] = r
    md = packet.assemble(ctx, owners, latest)
    result = faithfulness.check(md, ctx)

    version = conn.execute(
        "UPDATE weekly_runs SET packet_md = %s, packet_version = packet_version + 1"
        " WHERE id = %s AND client_id = %s RETURNING packet_version",
        (md, run_id, client_id),
    ).fetchone()["packet_version"]
    run["packet_version"] = version
    if result.passed:
        move(conn, run, "awaiting_approval")
    else:
        move(conn, run, "needs_attention", note="packet failed the faithfulness check")

    red = [k for k in ctx.kpis if k.status == "red"]
    return {
        "run_id": run_id,
        "client_id": client_id,
        "state": run["state"],
        "packet_version": version,
        "packet_md": md,
        "faithfulness": {"passed": result.passed, "problems": _problems_json(result)},
        "unanswered_kpis": [
            {
                "kpi_id": k.kpi_id,
                "name": k.name,
                "formula_key": k.formula_key,
                "owner_name": owners[k.kpi_id][0],
                "owner_email": email.recipient(owners[k.kpi_id][1]),
            }
            for k in red
            if k.kpi_id not in latest
        ],
    }


def record_approval(
    conn: psycopg.Connection,
    client_id: str,
    run_id: int,
    approver: str,
    decision: str,
    edits: str | None,
) -> dict:
    """Insert the Controller's decision on the current packet version (insert-only)."""
    run = get_run(conn, client_id, run_id, lock=True)
    _require_state(run, {"awaiting_approval"}, "record an approval")
    controller = conn.execute(
        "SELECT controller_email FROM clients WHERE id = %s", (client_id,)
    ).fetchone()["controller_email"]
    # TODO(signed approval tokens): REMOVE this whole approver-by-address check.
    # It trusts an address the caller sends, so it is not real identity. When
    # approvals come only through signed, single-use links, delete it and keep
    # the test that proves approving by address no longer works.
    # Interim: accept the Controller's address, or its test-mode alias.
    if approver.strip().lower() not in {controller.lower(), email.recipient(controller).lower()}:
        raise Forbidden("only the client's Controller can approve or reject the packet")
    approver = controller  # record the real address, not an alias
    if _approved(conn, run):
        raise Conflict("this packet version is already approved")
    row = conn.execute(
        "INSERT INTO approvals (run_id, approver, decision, edits, packet_version)"
        " VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (run_id, approver, decision, edits, run["packet_version"]),
    ).fetchone()
    if decision == "rejected":
        move(conn, run, "rejected", note=edits)
    return {"approval_id": row["id"], "run_id": run_id, "state": run["state"]}


def mark_sent(conn: psycopg.Connection, client_id: str, run_id: int) -> dict:
    run = get_run(conn, client_id, run_id, lock=True)
    _require_state(run, {"awaiting_approval"}, "mark the packet sent")
    if not _approved(conn, run):
        raise Conflict("the current packet version has no approved approval")
    move(conn, run, "sent")
    return {"run_id": run_id, "client_id": client_id, "state": run["state"]}


def needs_attention(conn: psycopg.Connection, client_id: str, run_id: int, reason: str) -> dict:
    run = get_run(conn, client_id, run_id, lock=True)
    move(conn, run, "needs_attention", note=reason)
    c = conn.execute(
        "SELECT name, controller_email FROM clients WHERE id = %s", (client_id,)
    ).fetchone()
    week = packet.week_label(run["week_start"])
    alert = email.compose(
        [c["controller_email"]],
        f"Needs attention: {c['name']}, week of {week}",
        f"The weekly KPI run for {c['name']} (week of {week}, run {run_id}) needs attention."
        f"\n\nReason: {reason}",
    )
    return {"run_id": run_id, "client_id": client_id, "state": run["state"], "alert_email": alert}
