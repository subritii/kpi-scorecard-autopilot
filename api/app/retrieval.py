"""pgvector search over last year's explanations. Always client-scoped.

Filter first, then rank: candidates are restricted to the client's (and
KPI's) past explanations dated before the current week, and only those are
ranked by cosine similarity. Results below RETRIEVAL_MIN_SIMILARITY are
dropped, so the app never claims "this happened before" without evidence.
"""

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

import psycopg


class RetrievalConfigError(Exception):
    """RETRIEVAL_MIN_SIMILARITY is missing or invalid."""


def min_similarity(env: Mapping[str, str] | None = None) -> float:
    """The similarity threshold from settings. Model-specific, so it has no default."""
    env = os.environ if env is None else env
    raw = env.get("RETRIEVAL_MIN_SIMILARITY", "")
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not -1 <= value <= 1:  # also rejects NaN
        raise RetrievalConfigError(
            "RETRIEVAL_MIN_SIMILARITY must be a cosine similarity between -1 and 1 "
            f"(tuned per embedding model); got {raw!r}"
        )
    return value


def to_vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text format, e.g. '[0.1,0.2]'. repr() keeps full float precision."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


@dataclass(frozen=True)
class Match:
    doc_id: int
    doc_key: str
    week_start: date
    similarity: float
    text: str


@dataclass(frozen=True)
class SearchResult:
    results: list[Match]  # at or above the threshold, best first
    best_similarity: float | None  # best score before the threshold, for tuning and logs


# MATERIALIZED makes Postgres run the filters first and rank only what they
# return. Without it, the planner may walk the HNSW index in similarity order
# and filter afterwards, which is approximate and can miss the client's rows.
SEARCH_SQL = """
WITH candidates AS MATERIALIZED (
    SELECT id, doc_key, week_start, text, embedding
    FROM doc_chunks
    WHERE client_id = %(client_id)s
      AND source_type = 'past_explanation'
      AND week_start < %(before)s::date
      AND embedding IS NOT NULL
      AND (%(kpi_id)s::int IS NULL OR kpi_id = %(kpi_id)s::int)
)
SELECT id, doc_key, week_start, text,
       1 - (embedding <=> %(query)s::vector) AS similarity  -- <=> is cosine distance
FROM candidates
ORDER BY embedding <=> %(query)s::vector, id                -- id breaks ties deterministically
LIMIT %(limit)s
"""


def search(
    conn: psycopg.Connection,
    client_id: str,
    query_vector: Sequence[float],
    before: date | str,
    kpi_id: int | None,
    limit: int = 3,
    min_similarity: float | None = None,
) -> SearchResult:
    """Past explanations for one client (and KPI, unless kpi_id is None), most similar first.

    kpi_id=None searches all of the client's KPIs (used by the live quality
    tests). min_similarity=None returns the raw ranking without a threshold.
    """
    if not any(query_vector):
        # An all-zero vector has no direction; cosine similarity is undefined.
        return SearchResult(results=[], best_similarity=None)
    rows = conn.execute(
        SEARCH_SQL,
        {
            "client_id": client_id,
            "before": before,
            "kpi_id": kpi_id,
            "query": to_vector_literal(query_vector),
            "limit": limit,
        },
    ).fetchall()
    matches = [
        Match(r["id"], r["doc_key"], r["week_start"], r["similarity"], r["text"])
        for r in rows
        if not math.isnan(r["similarity"])  # a stored zero vector scores NaN
    ]
    best = matches[0].similarity if matches else None
    if min_similarity is not None:
        matches = [m for m in matches if m.similarity >= min_similarity]
    return SearchResult(results=matches, best_similarity=best)
