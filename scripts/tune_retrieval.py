"""Measure retrieval scores with the configured embedding model and suggest a threshold.

    python scripts/tune_retrieval.py

Needs the seeded database and the real model (e.g. Ollama running). All
embeddings are written inside a transaction that is rolled back, so the
database is unchanged afterwards.

Scores two groups:
  correct: each planted event's query vs. last year's explanation of the same pattern
  wrong:   the same queries vs. the client's other past explanations, the
           Brightline AR query vs. the Northgate AR distractor, and a query
           with no history vs. every past explanation
and suggests RETRIEVAL_MIN_SIMILARITY as the midpoint of the gap between the
lowest correct and the highest wrong score. It prints the suggestion only;
update .env yourself. Exits with status 1 if there is no gap.
"""

import os
import sys

import psycopg
from embed_docs import sync_docs  # same folder
from psycopg import sql
from psycopg.rows import dict_row

from app import retrieval
from app.config import load_env
from app.eval_cases import DOCS, NO_HISTORY_QUERY, planted_cases
from app.llm import get_provider

DISTRACTOR = ("northgate", "past_explanation:ar_over_60:2025-08-11")


def scores(conn, client_id, vector) -> list[retrieval.Match]:
    """Every past explanation of the client, ranked, with no KPI filter and no threshold."""
    conn.execute(sql.SQL("SET LOCAL app.current_client_id = {}").format(sql.Literal(client_id)))
    found = retrieval.search(conn, client_id, vector, before="9999-01-04", kpi_id=None, limit=1000)
    return found.results


def main() -> int:
    load_env()
    provider = get_provider()
    cases = planted_cases()
    queries = [c.query for c in cases] + [NO_HISTORY_QUERY]
    correct, wrong = [], []

    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True) as conn:
        with conn.transaction(force_rollback=True):  # leave doc_chunks as it was
            sync_docs(conn, provider, DOCS)
            vectors = dict(zip(queries, provider.embed(queries, purpose="query"), strict=True))

            for c in cases:
                for m in scores(conn, c.client_id, vectors[c.query]):
                    label = f"{c.client_id} {c.formula_key} query -> {m.doc_key}"
                    (correct if m.doc_key == c.expected_doc_key else wrong).append(
                        (m.similarity, label)
                    )
            ar = next(
                c for c in cases if (c.client_id, c.formula_key) == ("brightline", "ar_over_60")
            )
            for m in scores(conn, DISTRACTOR[0], vectors[ar.query]):
                if m.doc_key == DISTRACTOR[1]:
                    wrong.append(
                        (m.similarity, "brightline ar_over_60 query -> northgate AR (cross-client)")
                    )
            for client_id in ("brightline", "northgate"):
                for m in scores(conn, client_id, vectors[NO_HISTORY_QUERY]):
                    wrong.append((m.similarity, f"no-history query -> {client_id} {m.doc_key}"))

    print(f"model: {provider.embedding_model}")
    print(
        f"current RETRIEVAL_MIN_SIMILARITY: {os.environ.get('RETRIEVAL_MIN_SIMILARITY', '(unset)')}"
    )
    print(f"\ncorrect ({len(correct)}), lowest first:")
    for s, label in sorted(correct):
        print(f"  {s:.4f}  {label}")
    print(f"\nwrong ({len(wrong)}), highest first (top 8):")
    for s, label in sorted(wrong, reverse=True)[:8]:
        print(f"  {s:.4f}  {label}")

    lowest_correct, highest_wrong = min(correct)[0], max(wrong)[0]
    gap = lowest_correct - highest_wrong
    print(
        f"\nlowest correct {lowest_correct:.4f}, highest wrong {highest_wrong:.4f}, gap {gap:.4f}"
    )
    if gap <= 0:
        print("no threshold separates them: improve the queries/documents or try another model")
        return 1
    print(f"suggested RETRIEVAL_MIN_SIMILARITY={(lowest_correct + highest_wrong) / 2:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
