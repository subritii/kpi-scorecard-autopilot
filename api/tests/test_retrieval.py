"""embed_docs + /retrieve plumbing, with the fake embedder (no model).

The fake embedder scores by shared words, so these tests check filtering,
isolation, thresholds and idempotency, not retrieval quality. Quality and
the threshold are measured by test_retrieval_live.py against real Ollama.

All database work runs as app_user in one transaction that is rolled back.
"""

import importlib.util
import os
from contextlib import contextmanager

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.rows import dict_row

from app import retrieval
from app.config import REPO_ROOT
from app.eval_cases import DOCS, NO_HISTORY_QUERY, planted_cases
from app.llm.base import EmbeddingDimensionError
from app.llm.fake import FakeProvider
from app.main import app, get_connect, get_llm

spec = importlib.util.spec_from_file_location("embed_docs", REPO_ROOT / "scripts" / "embed_docs.py")
embed_docs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(embed_docs)

client = TestClient(app, headers={"X-API-Key": os.environ["API_KEY"]})
FAKE_THRESHOLD = "0.2"  # the fake embedder's own scale; real models are tuned live
CASES = planted_cases()


def as_client(conn, client_id):
    conn.execute(sql.SQL("SET LOCAL app.current_client_id = {}").format(sql.Literal(client_id)))


@pytest.fixture
def fake():
    return FakeProvider(embedding_dim=int(os.environ.get("EMBEDDING_DIM", "768")))


@pytest.fixture
def db(fake, monkeypatch):
    """Seeded DB with all docs embedded by the fake provider, rolled back after the test."""
    try:
        conn = psycopg.connect(
            os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True, connect_timeout=2
        )
    except (KeyError, psycopg.OperationalError):
        pytest.skip("database not available")
    monkeypatch.setenv("RETRIEVAL_MIN_SIMILARITY", FAKE_THRESHOLD)

    @contextmanager
    def connect(client_id):
        as_client(conn, client_id)
        yield conn

    with conn.transaction(force_rollback=True):
        as_client(conn, "brightline")
        if not conn.execute("SELECT 1 FROM kpi_definitions LIMIT 1").fetchone():
            pytest.skip("seeded data not found; run scripts/seed_db.py")
        embed_docs.sync_docs(conn, fake, DOCS)
        app.dependency_overrides[get_connect] = lambda: connect
        app.dependency_overrides[get_llm] = lambda: fake
        try:
            yield conn
        finally:
            app.dependency_overrides.clear()
    conn.close()


def kpi_id(conn, client_id, formula_key) -> int:
    as_client(conn, client_id)
    return conn.execute(
        "SELECT id FROM kpi_definitions WHERE client_id = %s AND formula_key = %s",
        (client_id, formula_key),
    ).fetchone()["id"]


def retrieve(conn, client_id, formula_key, query, week_start="2026-08-03", **extra):
    body = {
        "client_id": client_id,
        "kpi_id": kpi_id(conn, client_id, formula_key),
        "week_start": week_start,
        "query": query,
        **extra,
    }
    return client.post("/retrieve", json=body)


def doc_count(conn, client_id) -> int:
    as_client(conn, client_id)
    return conn.execute(
        "SELECT count(*) AS n FROM doc_chunks WHERE client_id = %s", (client_id,)
    ).fetchone()["n"]


# --- embed_docs ----------------------------------------------------------------


def test_embed_docs_writes_every_doc_once(db, fake):
    # Brightline: 5 definitions + 2 notes + 4 past explanations; Northgate: 5 + 2 + 2.
    assert (doc_count(db, "brightline"), doc_count(db, "northgate")) == (11, 9)
    as_client(db, "brightline")
    rows = db.execute(
        "SELECT embedding IS NOT NULL AS has_vec, embedding_model FROM doc_chunks"
        " WHERE client_id = 'brightline'"
    ).fetchall()
    assert all(r["has_vec"] and r["embedding_model"] == fake.embedding_model for r in rows)


def test_embed_docs_rerun_creates_no_duplicates(db, fake):
    assert embed_docs.sync_docs(db, fake, DOCS) == {"embedded": 0, "unchanged": 20}
    assert (doc_count(db, "brightline"), doc_count(db, "northgate")) == (11, 9)


def test_embed_docs_reembeds_only_changed_docs(db, fake):
    changed = [dict(d) for d in DOCS]
    changed[0]["text"] += " Updated."
    assert embed_docs.sync_docs(db, fake, changed) == {"embedded": 1, "unchanged": 19}
    assert (doc_count(db, "brightline"), doc_count(db, "northgate")) == (11, 9)


def test_embed_docs_reembeds_everything_when_the_model_changes(db, fake):
    other = FakeProvider(embedding_dim=fake.embedding_dim)
    other.embedding_model = "another-model"
    assert embed_docs.sync_docs(db, other, DOCS) == {"embedded": 20, "unchanged": 0}


def test_embed_docs_links_documents_to_their_kpi(db):
    ar = kpi_id(db, "brightline", "ar_over_60")
    rows = {
        r["doc_key"]: r["kpi_id"]
        for r in db.execute("SELECT doc_key, kpi_id FROM doc_chunks WHERE client_id = 'brightline'")
    }
    assert rows["past_explanation:ar_over_60:2025-08-04"] == ar
    assert rows["note:general:undated"] is None


def test_embed_docs_dimension_mismatch_is_a_clear_error(db):
    with pytest.raises(EmbeddingDimensionError, match=r"vector\(\d+\).*EMBEDDING_DIM=8"):
        embed_docs.sync_docs(db, FakeProvider(embedding_dim=8), DOCS)


# --- /retrieve -------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c.client_id}-{c.formula_key}")
def test_planted_event_retrieves_its_own_past_explanation(db, case):
    resp = retrieve(db, case.client_id, case.formula_key, case.query, case.week_start)
    assert resp.status_code == 200
    results = resp.json()["results"]
    assert results and results[0]["doc_key"] == case.expected_doc_key


def test_northgate_ar_distractor_never_returned_for_brightline(db):
    # Even a query written to match the Northgate document word for word.
    northgate_text = next(
        d["text"] for d in DOCS if d["doc_key"] == "past_explanation:ar_over_60:2025-08-11"
    )
    body = retrieve(db, "brightline", "ar_over_60", northgate_text).json()
    assert all("Valemont" not in r["text"] for r in body["results"])
    # And not even when ranking across all of Brightline's KPIs.
    [vector] = FakeProvider(embedding_dim=db_dim(db)).embed([northgate_text], purpose="query")
    as_client(db, "brightline")
    found = retrieval.search(db, "brightline", vector, before="2026-08-03", kpi_id=None, limit=10)
    assert found.results and all("Valemont" not in m.text for m in found.results)


def db_dim(conn) -> int:
    return int(os.environ.get("EMBEDDING_DIM", "768"))


def test_no_client_set_returns_zero_results(db, fake):
    [vector] = fake.embed([CASES[0].query], purpose="query")
    # Sanity check: with the client set, the embedded docs are found.
    as_client(db, "brightline")
    assert retrieval.search(db, "brightline", vector, before="2026-08-03", kpi_id=None).results
    # Same connection, same docs, client setting cleared: RLS returns nothing,
    # even though the query itself filters on client_id = 'brightline'.
    db.execute("RESET app.current_client_id")
    found = retrieval.search(db, "brightline", vector, before="2026-08-03", kpi_id=None)
    assert found.results == [] and found.best_similarity is None


def test_query_with_no_similar_history_returns_nothing(db):
    body = retrieve(db, "brightline", "ar_over_60", NO_HISTORY_QUERY).json()
    assert body["results"] == []
    assert body["threshold"] == FAKE_THRESHOLD
    # The best score is still reported, so thresholds can be tuned from logs.
    assert body["best_similarity"] is None or float(body["best_similarity"]) < float(FAKE_THRESHOLD)


def test_only_explanations_from_before_the_current_week(db):
    case = next(c for c in CASES if c.formula_key == "ar_over_60")
    # The explanation is dated 2025-08-04, so asking from that same week finds nothing ...
    assert (
        retrieve(db, "brightline", "ar_over_60", case.query, "2025-08-04").json()["results"] == []
    )
    # ... and asking from the following week finds it.
    later = retrieve(db, "brightline", "ar_over_60", case.query, "2025-08-11").json()["results"]
    assert [r["doc_key"] for r in later] == [case.expected_doc_key]


def test_only_past_explanations_for_the_requested_kpi(db):
    # Copy the AR definition text word for word as the query: the definition
    # must not come back (definitions aren't history), nor any other KPI's doc.
    definition = next(d["text"] for d in DOCS if d["doc_key"] == "definition:ar_over_60:undated")
    results = retrieve(db, "brightline", "ar_over_60", definition).json()["results"]
    assert all(r["doc_key"].startswith("past_explanation:ar_over_60:") for r in results)


def test_threshold_comes_from_the_setting(db, monkeypatch):
    case = CASES[0]
    monkeypatch.setenv("RETRIEVAL_MIN_SIMILARITY", "0.99")
    body = retrieve(db, case.client_id, case.formula_key, case.query, case.week_start).json()
    assert body["threshold"] == "0.99"
    assert body["results"] == []


def test_results_shape_and_limit(db):
    case = CASES[0]
    body = retrieve(
        db, case.client_id, case.formula_key, case.query, case.week_start, limit=1
    ).json()
    [r] = body["results"]
    assert set(r) == {"doc_id", "doc_key", "week_start", "similarity", "text"}
    assert isinstance(r["similarity"], str) and len(r["similarity"].split(".")[1]) == 4


def test_stopword_only_query_returns_nothing(db):
    # Embeds to an all-zero vector with the fake provider; must not error.
    assert retrieve(db, "brightline", "ar_over_60", "the and of").json()["results"] == []


def test_other_clients_kpi_is_404(db):
    northgate_kpi = kpi_id(db, "northgate", "ar_over_60")
    resp = client.post(
        "/retrieve",
        json={
            "client_id": "brightline",
            "kpi_id": northgate_kpi,
            "week_start": "2026-08-03",
            "query": "AR rose",
        },
    )
    assert resp.status_code == 404


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_query_is_rejected(query):
    body = {"client_id": "brightline", "kpi_id": 1, "week_start": "2026-08-03", "query": query}
    assert client.post("/retrieve", json=body).status_code == 422


# --- threshold setting -------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "abc", "1.5", "-2"])
def test_bad_threshold_setting_is_a_clear_error(raw):
    env = {} if raw is None else {"RETRIEVAL_MIN_SIMILARITY": raw}
    with pytest.raises(retrieval.RetrievalConfigError, match="RETRIEVAL_MIN_SIMILARITY"):
        retrieval.min_similarity(env)


def test_threshold_setting_parses():
    assert retrieval.min_similarity({"RETRIEVAL_MIN_SIMILARITY": "0.62"}) == 0.62
