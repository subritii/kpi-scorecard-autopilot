-- Stable identity for each document, so scripts/embed_docs.py can re-run
-- without creating duplicates: it upserts on (client_id, doc_key).
--
-- embedding_model records which model produced the vector. A re-run skips
-- documents whose text and model are unchanged and re-embeds the rest, so
-- switching models never leaves a mix of incomparable vectors.
--
-- doc_chunks was empty when this was written, so NOT NULL is safe.

ALTER TABLE doc_chunks
    ADD COLUMN doc_key text NOT NULL,
    ADD COLUMN embedding_model text,
    ADD CONSTRAINT doc_chunks_client_doc_key UNIQUE (client_id, doc_key);
