"""Retrieval test cases built from the seed data, shared by the fake and live tests."""

import json
from dataclasses import dataclass

from app.config import REPO_ROOT

SEED = REPO_ROOT / "data" / "seed"
DOCS = json.loads((SEED / "docs.json").read_text())
GROUND_TRUTH = json.loads((SEED / "ground_truth.json").read_text())
KPI_NAMES = {
    (k["client_id"], k["formula_key"]): k["name"]
    for k in json.loads((SEED / "kpi_definitions.json").read_text())
}

# Something none of last year's explanations talk about.
NO_HISTORY_QUERY = "Office lease renewal and new furniture for the reception area."


@dataclass(frozen=True)
class Case:
    client_id: str
    formula_key: str
    week_start: str  # the planted week we're asking from
    query: str
    expected_doc_key: str  # last year's explanation of the same pattern


def planted_cases() -> list[Case]:
    """One query per planted event, phrased like the WF2 observation will be:
    what moved and which contributors drove it (no causes)."""
    cases = []
    for e in GROUND_TRUTH["events"]:
        [past] = [
            d
            for d in DOCS
            if d["client_id"] == e["client_id"]
            and d["formula_key"] == e["formula_key"]
            and d["source_type"] == "past_explanation"
        ]
        name = KPI_NAMES[(e["client_id"], e["formula_key"])]
        cases.append(
            Case(
                client_id=e["client_id"],
                formula_key=e["formula_key"],
                week_start=e["weeks"][0]["week_start"],
                query=f"{name} moved this week. Biggest movers: {', '.join(e['top_drivers'])}.",
                expected_doc_key=past["doc_key"],
            )
        )
    return cases
