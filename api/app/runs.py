"""Weekly run lifecycle: the state machine and every operation n8n triggers.

    collecting -> asking -> assembling -> awaiting_approval -> sent
                                     ^             |
                                     +- rejected <-+
    any open state -> needs_attention -> asking | assembling
    needs_attention -> sent | awaiting_approval   (only via resolve_claim)

Every state change goes through move(): it checks the transition is allowed,
locks the run row so two requests can't race, and appends to run_events.
Nothing is sent without an approval of the current packet version (checked
here and by a database trigger, rule 4).

Sending is claimed first (claim_send): one idempotency key per run, so a
retried n8n execution can't email twice. Once claimed, a run only moves to
sent (or needs_attention for a person to check): at most once, never twice.
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
    # sent / awaiting_approval only via resolve_claim, which requires an approved,
    # claimed packet: a person confirms whether a stuck send went out.
    "needs_attention": frozenset({"asking", "assembling", "sent", "awaiting_approval"}),
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
        "SELECT id, client_id, week_start, state, packet_md, packet_version,"
        " send_claim_key, send_claimed_at, send_claim_version FROM weekly_runs"
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
        # Derived: a claimed send that isn't marked sent yet is in flight (or stuck).
        "status": "sending"
        if run["send_claim_key"] is not None and run["state"] != "sent"
        else run["state"],
        "send_claimed_at": run["send_claimed_at"],
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
    _refuse_if_claimed(run, "rebuild the packet")
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
    _refuse_if_claimed(run, "record an approval")
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


def _refuse_if_claimed(run: dict, step: str) -> None:
    if run["send_claim_key"] is not None:
        raise Conflict(
            f"cannot {step}: the send was claimed at {run['send_claimed_at']}, so the packet "
            "may already be out; a person must check the leadership inbox first"
        )


def _packet_email(conn: psycopg.Connection, run: dict) -> dict:
    c = conn.execute(
        "SELECT name, leadership_emails FROM clients WHERE id = %s", (run["client_id"],)
    ).fetchone()
    week = packet.week_label(run["week_start"])
    return email.compose(
        c["leadership_emails"], f"KPI packet: {c['name']}, week of {week}", run["packet_md"]
    )


def claim_send(conn: psycopg.Connection, client_id: str, run_id: int, key: str) -> dict:
    """Claim the right to email the approved packet, before sending it.

    The first claim stores `key` (the n8n execution id). The same key again
    (a retried request) gets the same claim back; any other key is refused.
    """
    run = get_run(conn, client_id, run_id, lock=True)
    if run["state"] == "sent":
        raise Conflict("the packet was already sent; it must not be sent again")
    _require_state(run, {"awaiting_approval"}, "claim the send")
    if not _approved(conn, run):
        raise Conflict("the current packet version has no approved approval")
    repeated = run["send_claim_key"] is not None
    if repeated and run["send_claim_key"] != key:
        raise Conflict(
            f"the send was already claimed at {run['send_claimed_at']} by another execution"
        )
    if not repeated:
        row = conn.execute(
            "UPDATE weekly_runs SET send_claim_key = %s, send_claimed_at = clock_timestamp(),"
            " send_claim_version = packet_version WHERE id = %s AND client_id = %s"
            " RETURNING send_claimed_at",
            (key, run_id, client_id),
        ).fetchone()
        run["send_claimed_at"] = row["send_claimed_at"]
    return {
        "run_id": run_id,
        "client_id": client_id,
        "claimed": True,
        "repeated": repeated,
        "claimed_at": run["send_claimed_at"],
        "packet_version": run["packet_version"],
        "packet_email": _packet_email(conn, run),
    }


def mark_sent(conn: psycopg.Connection, client_id: str, run_id: int, key: str) -> dict:
    """Record that the packet went out. Safe to retry: an already-sent run is a success."""
    run = get_run(conn, client_id, run_id, lock=True)
    if run["state"] == "sent":
        return {"run_id": run_id, "client_id": client_id, "state": "sent", "already_sent": True}
    _require_state(run, {"awaiting_approval"}, "mark the packet sent")
    if not _approved(conn, run):
        raise Conflict("the current packet version has no approved approval")
    if run["send_claim_key"] is None:
        raise Conflict(
            "claim the send first (POST /runs/{id}/claim-send), then email, then mark sent"
        )
    if run["send_claim_key"] != key or run["send_claim_version"] != run["packet_version"]:
        raise Conflict("the send was claimed by another execution")
    move(conn, run, "sent")
    return {"run_id": run_id, "client_id": client_id, "state": "sent", "already_sent": False}


def needs_attention(conn: psycopg.Connection, client_id: str, run_id: int, reason: str) -> dict:
    run = get_run(conn, client_id, run_id, lock=True)
    move(conn, run, "needs_attention", note=reason)
    c = conn.execute(
        "SELECT name, controller_email FROM clients WHERE id = %s", (client_id,)
    ).fetchone()
    week = packet.week_label(run["week_start"])
    body = (
        f"The weekly KPI run for {c['name']} (week of {week}, run {run_id}) needs attention."
        f"\n\nReason: {reason}"
    )
    if run["send_claim_key"] is not None:
        body += (
            f"\n\nThe packet send was claimed at {run['send_claimed_at']} but never marked "
            "sent, so it may or may not have gone out. Check the leadership inbox, then "
            f"resolve it with POST /runs/{run_id}/resolve-claim and the body\n"
            f'  {{"client_id": "{client_id}", "resolution": "sent" or "not_sent", '
            '"resolved_by": "<your email>", "note": "<what you checked>"}\n'
            '"sent" marks the run sent; "not_sent" releases the claim so it can be sent again.'
        )
    alert = email.compose(
        [c["controller_email"]], f"Needs attention: {c['name']}, week of {week}", body
    )
    return {"run_id": run_id, "client_id": client_id, "state": run["state"], "alert_email": alert}


def resolve_claim(
    conn: psycopg.Connection,
    client_id: str,
    run_id: int,
    resolution: str,
    resolved_by: str,
    note: str | None,
) -> dict:
    """A person settles a stuck send claim, after checking the leadership inbox.

    "sent": the packet went out, so the run becomes sent. "not_sent": it didn't,
    so the claim is released and the run is awaiting_approval again, ready to be
    claimed by a new execution. Recorded in claim_resolutions (append-only).
    """
    run = get_run(conn, client_id, run_id, lock=True)
    if run["state"] == "sent":
        raise Conflict("the run is already sent; there is no claim to resolve")
    if run["send_claim_key"] is None:
        raise Conflict("there is no send claim to resolve")
    _require_state(run, {"awaiting_approval", "needs_attention"}, "resolve the claim")
    if not _approved(conn, run):
        raise Conflict("the current packet version has no approved approval")
    conn.execute(
        "INSERT INTO claim_resolutions (run_id, resolution, resolved_by, note, claim_key)"
        " VALUES (%s, %s, %s, %s, %s)",
        (run_id, resolution, resolved_by, note, run["send_claim_key"]),
    )
    note_text = f"claim resolved as {resolution} by {resolved_by}"
    if resolution == "sent":
        move(conn, run, "sent", note=note_text)
    else:
        conn.execute(
            "UPDATE weekly_runs SET send_claim_key = NULL, send_claimed_at = NULL,"
            " send_claim_version = NULL WHERE id = %s AND client_id = %s",
            (run_id, client_id),
        )
        if run["state"] == "needs_attention":
            move(conn, run, "awaiting_approval", note=note_text)
    return {
        "run_id": run_id,
        "client_id": client_id,
        "state": run["state"],
        "resolution": resolution,
    }
