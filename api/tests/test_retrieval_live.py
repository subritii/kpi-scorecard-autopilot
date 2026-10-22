"""Retrieval quality with the real embedding model. Run with: pytest --live -s

Needs Ollama running on the host with the embedding model pulled, plus the
seeded database. Embeds all docs inside a rolled-back transaction, then for
each planted event checks that last year's explanation of the same pattern
ranks first among ALL of that client's past explanations (stricter than the
API, which only searches the KPI's own), clears RETRIEVAL_MIN_SIMILARITY,
and that a query with no history stays below it. -s prints the scores used
to tune the threshold.
"""

import importlib.util
import os

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from app import retrieval
from app.config import REPO_ROOT
from app.eval_cases import DOCS, NO_HISTORY_QUERY, planted_cases
from app.llm import get_provider

pytestmark = pytest.mark.live

spec = importlib.util.spec_from_file_location("embed_docs", REPO_ROOT / "scripts" / "embed_docs.py")
embed_docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(embed_docs)


@pytest.fixture(scope="module")
def live():
    """(connection, provider, query vectors) with real embeddings, rolled back at the end."""
    provider = get_provider()
    conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)
    with conn.transaction(force_rollback=True):
        embed_docs.sync_docs(conn, provider, DOCS)
        queries = [c.query for c in planted_cases()] + [NO_HISTORY_QUERY]
        vectors = dict(zip(queries, provider.embed(queries, purpose="query"), strict=True))
        yield conn, vectors
    conn.close()


def search(conn, client_id, vector, kpi_id=None):
    conn.execute(sql.SQL("SET LOCAL app.current_client_id = {}").format(sql.Literal(client_id)))
    return retrieval.search(conn, client_id, vector, before="2026-07-06", kpi_id=kpi_id, limit=10)


def test_planted_events_rank_their_own_explanation_first(live):
    conn, vectors = live
    threshold = retrieval.min_similarity()
    print(f"\nthreshold RETRIEVAL_MIN_SIMILARITY={threshold}")
    print(f"{'event':<28} {'own':>6} {'next best':>10}  next best doc")
    failures = []
    for case in planted_cases():
        found = search(conn, case.client_id, vectors[case.query])
        own = next(m for m in found.results if m.doc_key == case.expected_doc_key)
        others = [m for m in found.results if m.doc_key != case.expected_doc_key]
        runner_up = others[0] if others else None
        print(
            f"{case.client_id + ' ' + case.formula_key:<28} {own.similarity:>6.3f} "
            f"{runner_up.similarity if runner_up else float('nan'):>10.3f}  "
            f"{runner_up.doc_key if runner_up else '-'}"
        )
        if found.results[0].doc_key != case.expected_doc_key:
            failures.append(f"{case.formula_key}: top was {found.results[0].doc_key}")
        if own.similarity < threshold:
            failures.append(f"{case.formula_key}: own {own.similarity:.3f} < {threshold}")
    assert not failures, failures


def test_no_history_query_stays_below_threshold(live):
    conn, vectors = live
    threshold = retrieval.min_similarity()
    for client_id in ("brightline", "northgate"):
        found = search(conn, client_id, vectors[NO_HISTORY_QUERY])
        print(f"\nno-history best for {client_id}: {found.best_similarity:.3f}")
        assert found.best_similarity < threshold
