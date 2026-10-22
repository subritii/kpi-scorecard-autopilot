"""FastAPI app for KPI Scorecard Autopilot.

n8n calls these endpoints; all KPI math lives here in Python, never in an LLM prompt.
Money and percentages are returned as strings (e.g. "131000.50") so no JSON
client can turn them into floats.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date
from typing import Annotated

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import service
from app.db import client_connection

app = FastAPI(title="KPI Scorecard Autopilot API", version="0.1.0")


def get_connect():
    """Dependency: how routes open a client-scoped connection. Tests swap it out."""
    return client_connection


# A function that opens a connection scoped to the given client_id.
Connect = Annotated[
    Callable[[str], AbstractContextManager[psycopg.Connection]], Depends(get_connect)
]


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
