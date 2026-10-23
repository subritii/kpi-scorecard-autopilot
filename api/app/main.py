"""FastAPI app for KPI Scorecard Autopilot.

n8n calls these endpoints; all KPI math lives here in Python, never in an LLM prompt.
Money and percentages are returned as strings (e.g. "131000.50") so no JSON
client can turn them into floats.
"""

import os
from collections.abc import Callable
from contextlib import AbstractContextManager, asynccontextmanager
from datetime import date
from functools import lru_cache
from typing import Annotated, Literal

import psycopg
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from app import email, faithfulness, links, pages, retrieval, runs, service
from app.auth import check_configured_key, require_api_key
from app.db import client_connection
from app.llm import get_provider
from app.llm.base import LLMConfigError, LLMError, LLMProvider


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse to start on bad settings, rather than failing per request.
    check_configured_key()
    email.check_override()
    links.check_settings()
    yield


# FastAPI's built-in docs are off; see /docs below (development only).
app = FastAPI(
    title="KPI Scorecard Autopilot API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
# Every API route lives on this router and needs the API key (app/auth.py).
api = APIRouter(dependencies=[Depends(require_api_key)])


@app.exception_handler(LLMError)
def llm_error(request: Request, exc: LLMError) -> JSONResponse:
    # Misconfiguration is our fault (500); a failing model server is upstream (502).
    # Messages never contain secrets (see app/llm/base.py).
    status = 500 if isinstance(exc, LLMConfigError) else 502
    return JSONResponse(status_code=status, content={"detail": str(exc)})


@app.exception_handler(runs.RunNotFound)
def run_not_found(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(runs.Conflict)
def run_conflict(request: Request, exc: Exception) -> JSONResponse:
    # Includes InvalidTransition: the run's state doesn't allow this step.
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(email.EmailConfigError)
@app.exception_handler(links.LinkConfigError)
@app.exception_handler(retrieval.RetrievalConfigError)
def retrieval_config_error(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=500, content={"detail": str(exc)})


def get_connect():
    """Dependency: how routes open a client-scoped connection. Tests swap it out."""
    return client_connection


# A function that opens a connection scoped to the given client_id.
Connect = Annotated[
    Callable[[str], AbstractContextManager[psycopg.Connection]], Depends(get_connect)
]


@lru_cache(maxsize=1)
def _provider() -> LLMProvider:
    # Built once from .env and reused (it holds an HTTP connection pool).
    return get_provider()


def get_llm() -> LLMProvider:
    """Dependency: the configured LLM provider. Tests swap in the fake one."""
    return _provider()


LLM = Annotated[LLMProvider, Depends(get_llm)]


# --- request models ------------------------------------------------------------


class WeekRequest(BaseModel):
    # Unknown fields are rejected instead of silently ignored.
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)
    week_start: date

    @field_validator("week_start")
    @classmethod
    def must_be_monday(cls, v: date) -> date:
        if v.isoweekday() != 1:
            raise ValueError("week_start must be a Monday")
        return v


class DriversRequest(WeekRequest):
    kpi_id: int
    limit: int | None = Field(default=None, ge=1)


class RetrieveRequest(WeekRequest):
    kpi_id: int
    query: str = Field(min_length=1)  # e.g. the WF2 observation of what moved
    limit: int = Field(default=3, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank")
        return v


# --- response models: every number is a string ---------------------------------


class KpiResult(BaseModel):
    kpi_id: int
    name: str
    formula_key: str
    value: str | None  # exact, unrounded
    display_value: str | None  # rounded for people (1 dp for % and days, cents for money)
    prior_value: str | None
    status: str
    target: str
    direction: str
    yellow_band_pct: str


class ComputeResponse(BaseModel):
    client_id: str
    week_start: date
    kpis: list[KpiResult]


class DriverOut(BaseModel):
    name: str
    prior: str
    current: str
    change: str
    share_of_movement: str | None


class DenominatorOut(BaseModel):
    metric: str
    prior: str
    current: str
    change: str
    effect_on_kpi: str | None
    top_contributor_effect_on_kpi: str | None
    exceeds_top_contributor: bool | None


class MatchOut(BaseModel):
    doc_id: int
    doc_key: str
    week_start: date
    similarity: str  # cosine similarity, 4 decimal places
    text: str


class RetrieveResponse(BaseModel):
    client_id: str
    kpi_id: int
    week_start: date
    threshold: str
    best_similarity: str | None  # reported even when below the threshold, for tuning
    results: list[MatchOut]


class FaithfulnessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)  # every connection is client-scoped (RLS)
    run_id: int
    text: str = Field(min_length=1)  # the packet as Markdown, owner quotes included


class ProblemOut(BaseModel):
    type: str
    text: str
    reason: str
    start: int


class FaithfulnessResponse(BaseModel):
    client_id: str
    run_id: int
    passed: bool
    problems: list[ProblemOut]


class DriversResponse(BaseModel):
    client_id: str
    kpi_id: int
    week_start: date
    formula_key: str
    drivers: list[DriverOut]
    net_change: str | None
    contributors_net_change: str
    denominator: DenominatorOut | None
    warnings: list[str]


# --- routes --------------------------------------------------------------------


@api.get("/health")
def health() -> dict[str, str]:
    # Liveness only: confirms the process is up. Needs the API key like every route.
    return {"status": "ok"}


@api.post("/compute", response_model=ComputeResponse)
def compute(req: WeekRequest, connect: Connect):
    """Compute all KPIs for a client-week and write them to kpi_values."""
    with connect(req.client_id) as conn:
        results = service.compute_week(conn, req.client_id, req.week_start)
    if results is None:
        raise HTTPException(status_code=404, detail="no KPI definitions for this client")
    return {
        "client_id": req.client_id,
        "week_start": req.week_start,
        "kpis": service.as_json(results),
    }


@api.post("/drivers", response_model=DriversResponse)
def drivers(req: DriversRequest, connect: Connect):
    """Ranked week-over-week contributors to one KPI."""
    with connect(req.client_id) as conn:
        result = service.drivers_for_kpi(conn, req.client_id, req.kpi_id, req.week_start, req.limit)
    if result is None:
        raise HTTPException(status_code=404, detail="KPI not found for this client")
    return {
        "client_id": req.client_id,
        "kpi_id": req.kpi_id,
        "week_start": req.week_start,
        **service.drivers_output(result),
    }


@api.post("/retrieve", response_model=RetrieveResponse)
def retrieve(req: RetrieveRequest, connect: Connect, llm: LLM):
    """Last year's explanations of a similar move in this KPI, if any are close enough.

    Only past explanations of this client's KPI, dated before week_start, are
    considered. Nothing comes back unless the match clears the threshold.
    """
    threshold = retrieval.min_similarity()
    with connect(req.client_id) as conn:
        if not service.kpi_exists(conn, req.client_id, req.kpi_id):
            raise HTTPException(status_code=404, detail="KPI not found for this client")
    # Embed outside any database transaction: model calls can be slow.
    [vector] = llm.embed([req.query], purpose="query")
    with connect(req.client_id) as conn:
        found = retrieval.search(
            conn,
            req.client_id,
            vector,
            before=req.week_start,
            kpi_id=req.kpi_id,
            limit=req.limit,
            min_similarity=threshold,
        )
    return {
        "client_id": req.client_id,
        "kpi_id": req.kpi_id,
        "week_start": req.week_start,
        "threshold": str(threshold),
        "best_similarity": None
        if found.best_similarity is None
        else f"{found.best_similarity:.4f}",
        "results": [
            {
                "doc_id": m.doc_id,
                "doc_key": m.doc_key,
                "week_start": m.week_start,
                "similarity": f"{m.similarity:.4f}",
                "text": m.text,
            }
            for m in found.results
        ],
    }


@api.post("/check-faithfulness", response_model=FaithfulnessResponse)
def check_faithfulness(req: FaithfulnessRequest, connect: Connect):
    """Check AI-written packet text against the run's data. A safety net before the
    Controller's review, not a replacement for it."""
    with connect(req.client_id) as conn:
        try:
            ctx = service.run_context(conn, req.client_id, req.run_id)
        except service.NotComputed as e:
            raise HTTPException(
                status_code=409, detail=f"{e}; run POST /compute for that week first"
            ) from e
    if ctx is None:
        raise HTTPException(status_code=404, detail="run not found for this client")
    result = faithfulness.check(req.text, ctx)
    return {
        "client_id": req.client_id,
        "run_id": req.run_id,
        "passed": result.passed,
        "problems": [
            {"type": p.type, "text": p.text, "reason": p.reason, "start": p.start}
            for p in result.problems
        ],
    }


# --- run lifecycle (called by n8n; n8n never touches the database or the LLM) --------


class ClientRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: str = Field(min_length=1)


class NonBlank(BaseModel):
    @field_validator("*", mode="after")
    @classmethod
    def not_blank(cls, v):
        if isinstance(v, str) and not v.strip():
            raise ValueError("must not be blank")
        return v


class ResponseRequest(ClientRequest, NonBlank):
    kpi_id: int
    what_changed: str = Field(min_length=1)
    why: str = Field(min_length=1)
    owner_of_fix: str = Field(min_length=1)
    actions: str = Field(min_length=1)


class SendRequest(ClientRequest, NonBlank):
    # Identifies the sender, e.g. the n8n execution id; reused on a retry.
    idempotency_key: str = Field(min_length=8, max_length=200)


class ResolveClaimRequest(ClientRequest, NonBlank):
    resolution: Literal["sent", "not_sent"]
    resolved_by: str = Field(min_length=1)  # who checked the leadership inbox
    note: str | None = None


class AttentionRequest(ClientRequest, NonBlank):
    reason: str = Field(min_length=1)


@api.post("/runs/start")
def runs_start(req: WeekRequest, connect: Connect):
    """Compute the week, create its run (collecting -> asking), return the red KPIs."""
    with connect(req.client_id) as conn:
        return service.as_json(runs.start(conn, req.client_id, req.week_start))


@api.get("/runs/ready-to-send")
def runs_ready_to_send(client_id: str, connect: Connect):
    """Runs approved and not yet claimed: the send workflow's poll."""
    with connect(client_id) as conn:
        return service.as_json(runs.ready_to_send(conn, client_id))


@api.get("/runs/{run_id}")
def runs_get(run_id: int, client_id: str, connect: Connect):
    """State, packet, recipients, approval and unanswered KPIs, for n8n."""
    with connect(client_id) as conn:
        return service.as_json(runs.describe(conn, client_id, run_id))


@api.post("/runs/{run_id}/observations")
def runs_observations(run_id: int, req: ClientRequest, connect: Connect, llm: LLM):
    """An owner message per red KPI: LLM observation (checked, one retry) or template."""
    return service.as_json(runs.observations(connect, llm, req.client_id, run_id))


@api.post("/runs/{run_id}/responses")
def runs_responses(run_id: int, req: ResponseRequest, connect: Connect):
    """Save an owner's reply, verbatim."""
    reply = req.model_dump(exclude={"client_id"})
    with connect(req.client_id) as conn:
        return runs.save_response(conn, req.client_id, run_id, reply)


@api.post("/runs/{run_id}/packet")
def runs_packet(run_id: int, req: ClientRequest, connect: Connect):
    """Assemble and check the packet; awaiting_approval if it passes."""
    with connect(req.client_id) as conn:
        return service.as_json(runs.build_packet(conn, req.client_id, run_id))


@api.post("/runs/{run_id}/approval-email")
def runs_approval_email(run_id: int, req: ClientRequest, connect: Connect):
    """A fresh approval email (single-use link) for the Controller, e.g. to resend it."""
    with connect(req.client_id) as conn:
        return service.as_json(runs.approval_email(conn, req.client_id, run_id))


@api.post("/runs/{run_id}/reminders")
def runs_reminders(run_id: int, req: ClientRequest, connect: Connect):
    """Reminder emails, with fresh reply links, for red KPIs that have no reply yet."""
    with connect(req.client_id) as conn:
        return runs.reminders(conn, req.client_id, run_id)


@api.post("/runs/{run_id}/claim-send")
def runs_claim_send(run_id: int, req: SendRequest, connect: Connect):
    """Claim the send of the approved packet; returns the email to send. Call before emailing."""
    with connect(req.client_id) as conn:
        return service.as_json(runs.claim_send(conn, req.client_id, run_id, req.idempotency_key))


@api.post("/runs/{run_id}/mark-sent")
def runs_mark_sent(run_id: int, req: SendRequest, connect: Connect):
    """Mark the run sent, with the claim's key. An already-sent run returns success."""
    with connect(req.client_id) as conn:
        return runs.mark_sent(conn, req.client_id, run_id, req.idempotency_key)


@api.post("/runs/{run_id}/resolve-claim")
def runs_resolve_claim(run_id: int, req: ResolveClaimRequest, connect: Connect):
    """Settle a stuck send claim: the packet went out ("sent") or it didn't ("not_sent")."""
    with connect(req.client_id) as conn:
        return runs.resolve_claim(
            conn, req.client_id, run_id, req.resolution, req.resolved_by, req.note
        )


@api.post("/runs/{run_id}/needs-attention")
def runs_needs_attention(run_id: int, req: AttentionRequest, connect: Connect):
    """Flag the run for a human (WF6 error handler), with the reason logged."""
    with connect(req.client_id) as conn:
        return runs.needs_attention(conn, req.client_id, run_id, req.reason)


# --- pages for people: approval and owner reply ------------------------------------------
# Opened from an email in a browser, which can't send the API key, so these
# routes are outside the key-protected router and authenticate with the signed,
# single-use token in the link instead (app/links.py). GET only shows a page;
# only POST (submitting the form) changes anything, so link scanners are harmless.


def _html(body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status, headers=pages.HEADERS)


def _link_error(e: Exception) -> HTMLResponse:
    if isinstance(e, links.LinkGone):
        return _html(pages.message("Link no longer valid", str(e)), 410)
    if isinstance(e, links.InvalidLink):
        return _html(pages.message("Invalid link", "This link is not valid."), 403)
    if isinstance(e, runs.Conflict):
        return _html(pages.message("Nothing to do", str(e)), 409)
    raise e


def _with_link(connect, token: str, work):
    """Verify the token, then run work(conn, link) as that link's client."""
    try:
        link = links.verify(token)
        with connect(link.client_id) as conn:
            return work(conn, link)
    except (links.InvalidLink, links.LinkGone, runs.Conflict) as e:
        return _link_error(e)


@app.get("/approve", include_in_schema=False)
def approve_page(connect: Connect, token: str = ""):
    def show(conn, link):
        info = runs.approval_page(conn, link)
        return _html(pages.approval(token, info["client_name"], info["week"], info["packet_html"]))

    return _with_link(connect, token, show)


@app.post("/approve", include_in_schema=False)
async def approve_submit(request: Request, connect: Connect):
    form = pages.parse_form(await request.body())
    decision, notes = form.get("decision", ""), form.get("notes", "").strip()
    if decision not in ("approved", "rejected"):
        return _html(pages.message("Choose approve or reject", "Use the buttons on the page."), 422)

    def decide(conn, link):
        result = runs.decide_by_link(conn, link, decision, notes)
        word = "approved" if result["decision"] == "approved" else "rejected"
        return _html(pages.message(f"Packet {word}", f"Thank you. The packet was {word}."))

    return await run_in_threadpool(_with_link, connect, form.get("token", ""), decide)


@app.get("/reply", include_in_schema=False)
def reply_page(connect: Connect, token: str = ""):
    def show(conn, link):
        info = runs.reply_page(conn, link)
        return _html(pages.reply_form(token, info["kpi_name"], info["week"], info["owner_name"]))

    return _with_link(connect, token, show)


@app.post("/reply", include_in_schema=False)
async def reply_submit(request: Request, connect: Connect):
    form = pages.parse_form(await request.body())
    token = form.get("token", "")
    fields = {name: form.get(name, "") for name, _ in pages.REPLY_FIELDS}

    def save(conn, link):
        if any(not v.strip() for v in fields.values()):
            # Show the form again with what they typed; the link stays unused.
            info = runs.reply_page(conn, link)
            return _html(
                pages.reply_form(
                    token,
                    info["kpi_name"],
                    info["week"],
                    info["owner_name"],
                    values=fields,
                    error="Please answer all four questions.",
                ),
                422,
            )
        runs.reply_by_link(conn, link, fields)
        return _html(pages.message("Thank you", "Your reply was saved and goes into the packet."))

    return await run_in_threadpool(_with_link, connect, token, save)


# --- development docs ------------------------------------------------------------------
# Off unless DOCS_ENABLED is set. These are the only routes without the API key
# (so a browser can open them), which is why they must stay off outside development.


def docs_enabled() -> bool:
    return os.environ.get("DOCS_ENABLED", "").strip().lower() in {"1", "true", "yes"}


@app.get("/openapi.json", include_in_schema=False)
def openapi_schema():
    if not docs_enabled():
        raise HTTPException(status_code=404)
    return app.openapi()


@app.get("/docs", include_in_schema=False)
def swagger_ui() -> HTMLResponse:
    if not docs_enabled():
        raise HTTPException(status_code=404)
    return get_swagger_ui_html(openapi_url="/openapi.json", title=app.title)


# Mount the key-protected API routes on the app.
app.include_router(api)
