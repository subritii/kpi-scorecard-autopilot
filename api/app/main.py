"""FastAPI app for KPI Scorecard Autopilot.

n8n calls these endpoints; all KPI math lives here in Python, never in an LLM prompt.
Money and percentages are returned as strings (e.g. "131000.50") so no JSON
client can turn them into floats.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date
from functools import lru_cache
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import faithfulness, retrieval, runs, service
from app.db import client_connection
from app.llm import get_provider
from app.llm.base import LLMConfigError, LLMError, LLMProvider

app = FastAPI(title="KPI Scorecard Autopilot API", version="0.1.0")


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


@app.exception_handler(runs.Forbidden)
def run_forbidden(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": str(exc)})


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


@app.get("/health")
def health() -> dict[str, str]:
    # Liveness only: confirms the process is up.
    return {"status": "ok"}


@app.post("/compute", response_model=ComputeResponse)
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


@app.post("/drivers", response_model=DriversResponse)
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


@app.post("/retrieve", response_model=RetrieveResponse)
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


@app.post("/check-faithfulness", response_model=FaithfulnessResponse)
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


class ApprovalRequest(ClientRequest, NonBlank):
    approver: str = Field(min_length=1)  # must be the client's Controller
    decision: Literal["approved", "rejected"]
    edits: str | None = None  # the Controller's notes; the packet itself is not edited


class AttentionRequest(ClientRequest, NonBlank):
    reason: str = Field(min_length=1)


@app.post("/runs/start")
def runs_start(req: WeekRequest, connect: Connect):
    """Compute the week, create its run (collecting -> asking), return the red KPIs."""
    with connect(req.client_id) as conn:
        return service.as_json(runs.start(conn, req.client_id, req.week_start))


@app.get("/runs/{run_id}")
def runs_get(run_id: int, client_id: str, connect: Connect):
    """State, packet, recipients, approval and unanswered KPIs, for n8n."""
    with connect(client_id) as conn:
        return service.as_json(runs.describe(conn, client_id, run_id))


@app.post("/runs/{run_id}/observations")
def runs_observations(run_id: int, req: ClientRequest, connect: Connect, llm: LLM):
    """An owner message per red KPI: LLM observation (checked, one retry) or template."""
    return service.as_json(runs.observations(connect, llm, req.client_id, run_id))


@app.post("/runs/{run_id}/responses")
def runs_responses(run_id: int, req: ResponseRequest, connect: Connect):
    """Save an owner's reply, verbatim."""
    reply = req.model_dump(exclude={"client_id"})
    with connect(req.client_id) as conn:
        return runs.save_response(conn, req.client_id, run_id, reply)


@app.post("/runs/{run_id}/packet")
def runs_packet(run_id: int, req: ClientRequest, connect: Connect):
    """Assemble and check the packet; awaiting_approval if it passes."""
    with connect(req.client_id) as conn:
        return service.as_json(runs.build_packet(conn, req.client_id, run_id))


@app.post("/runs/{run_id}/approval")
def runs_approval(run_id: int, req: ApprovalRequest, connect: Connect):
    """Record the Controller's decision on the current packet version (insert-only)."""
    with connect(req.client_id) as conn:
        return runs.record_approval(
            conn, req.client_id, run_id, req.approver, req.decision, req.edits
        )


@app.post("/runs/{run_id}/mark-sent")
def runs_mark_sent(run_id: int, req: ClientRequest, connect: Connect):
    """Mark the run sent. 409 unless the current packet version is approved."""
    with connect(req.client_id) as conn:
        return runs.mark_sent(conn, req.client_id, run_id)


@app.post("/runs/{run_id}/needs-attention")
def runs_needs_attention(run_id: int, req: AttentionRequest, connect: Connect):
    """Flag the run for a human (WF6 error handler), with the reason logged."""
    with connect(req.client_id) as conn:
        return runs.needs_attention(conn, req.client_id, run_id, req.reason)
