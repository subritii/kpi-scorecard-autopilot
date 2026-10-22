"""Embed KPI definitions, notes and past explanations into doc_chunks.

Runs as the admin role (an operator task, like seeding). Safe to re-run:
each document has a stable doc_key and is upserted on (client_id, doc_key).
Documents whose text and embedding model are unchanged are skipped; changed
ones (or all of them, after a model switch) are re-embedded in place.
Run scripts/seed_db.py first, so the KPI ids exist.
"""

import json
import os

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from app.config import REPO_ROOT, load_env
from app.llm import get_provider
from app.llm.base import EmbeddingDimensionError, LLMProvider
from app.retrieval import to_vector_literal

DOCS_PATH = REPO_ROOT / "data" / "seed" / "docs.json"
BATCH_SIZE = 32  # texts per embedding request

UPSERT_SQL = """
INSERT INTO doc_chunks
    (client_id, doc_key, kpi_id, week_start, source_type, text, embedding, embedding_model)
VALUES (%s, %s, %s, %s::date, %s, %s, %s::vector, %s)
ON CONFLICT (client_id, doc_key) DO UPDATE SET
    kpi_id = EXCLUDED.kpi_id,
    week_start = EXCLUDED.week_start,
    source_type = EXCLUDED.source_type,
    text = EXCLUDED.text,
    embedding = EXCLUDED.embedding,
    embedding_model = EXCLUDED.embedding_model
"""


def check_dimension(conn: psycopg.Connection, provider: LLMProvider) -> None:
    """Fail before embedding anything if the column size doesn't match EMBEDDING_DIM."""
    column_type = conn.execute(
        "SELECT format_type(atttypid, atttypmod) AS t FROM pg_attribute"
        " WHERE attrelid = 'doc_chunks'::regclass AND attname = 'embedding'"
    ).fetchone()["t"]
    if column_type != f"vector({provider.embedding_dim})":
        raise EmbeddingDimensionError(
            f"doc_chunks.embedding is {column_type} but EMBEDDING_DIM={provider.embedding_dim}. "
            "The column size is fixed when migrations run; use a model with that output "
            "size, or rebuild doc_chunks for the new size."
        )


def sync_docs(conn: psycopg.Connection, provider: LLMProvider, docs: list[dict]) -> dict:
    """Embed new or changed docs and upsert them. Returns {"embedded": n, "unchanged": m}."""
    check_dimension(conn, provider)
    counts = {"embedded": 0, "unchanged": 0}

    for client_id in sorted({d["client_id"] for d in docs}):
        # Scope to the client so this also works under RLS (as app_user in
        # tests); the admin role ignores it. Every query also filters by client_id.
        conn.execute(sql.SQL("SET LOCAL app.current_client_id = {}").format(sql.Literal(client_id)))
        client_docs = [d for d in docs if d["client_id"] == client_id]
        kpi_ids = {
            r["formula_key"]: r["id"]
            for r in conn.execute(
                "SELECT id, formula_key FROM kpi_definitions WHERE client_id = %s", (client_id,)
            )
        }
        unknown = [d["doc_key"] for d in client_docs if d["formula_key"] not in (*kpi_ids, None)]
        if unknown:
            raise ValueError(f"{client_id}: docs refer to KPIs it doesn't have: {unknown}")
        existing = {
            r["doc_key"]: (r["text"], r["embedding_model"])
            for r in conn.execute(
                "SELECT doc_key, text, embedding_model FROM doc_chunks"
                " WHERE client_id = %s AND embedding IS NOT NULL",
                (client_id,),
            )
        }
        todo = [
            d
            for d in client_docs
            if existing.get(d["doc_key"]) != (d["text"], provider.embedding_model)
        ]
        counts["unchanged"] += len(client_docs) - len(todo)

        for start in range(0, len(todo), BATCH_SIZE):
            batch = todo[start : start + BATCH_SIZE]
            vectors = provider.embed([d["text"] for d in batch], purpose="document")
            for d, vector in zip(batch, vectors, strict=True):
                conn.execute(
                    UPSERT_SQL,
                    (
                        client_id,
                        d["doc_key"],
                        kpi_ids.get(d["formula_key"]),
                        d["week_start"],
                        d["source_type"],
                        d["text"],
                        to_vector_literal(vector),
                        provider.embedding_model,
                    ),
                )
        counts["embedded"] += len(todo)
    return counts


def main() -> None:
    load_env()
    provider = get_provider()
    docs = json.loads(DOCS_PATH.read_text())
    # Leaving the `with` block commits once; any error rolls back everything.
    with psycopg.connect(os.environ["ADMIN_DATABASE_URL"], row_factory=dict_row) as conn:
        counts = sync_docs(conn, provider, docs)
    model = provider.embedding_model
    print(f"{model}: {counts['embedded']} embedded, {counts['unchanged']} unchanged")


if __name__ == "__main__":
    main()
