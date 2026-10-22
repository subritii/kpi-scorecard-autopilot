-- Row-level security: a connection only sees rows for the client named in
-- the session setting app.current_client_id (set with SET LOCAL by api/app/db.py).
--
-- current_setting('app.current_client_id', true): the `true` (missing_ok)
-- makes it return NULL instead of raising when the setting was never set, and
-- `client_id = NULL` matches nothing. After a SET LOCAL transaction ends, the
-- setting reads as '' for the rest of the session, which also matches no
-- client id. Either way an unscoped connection gets zero rows, not an error.
--
-- The role the API uses (app_user) and FORCE ROW LEVEL SECURITY are in
-- 004_app_user.sql.

ALTER TABLE clients         ENABLE ROW LEVEL SECURITY;
ALTER TABLE kpi_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_facts    ENABLE ROW LEVEL SECURITY;
ALTER TABLE kpi_values      ENABLE ROW LEVEL SECURITY;
ALTER TABLE weekly_runs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE owner_responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals       ENABLE ROW LEVEL SECURITY;
ALTER TABLE doc_chunks      ENABLE ROW LEVEL SECURITY;

-- In every policy, USING filters which rows can be read, updated or deleted;
-- WITH CHECK rejects inserts/updates that would write a row for another client.

-- clients: a session sees only its own client record (the key here is id, not client_id).
CREATE POLICY client_isolation ON clients
    USING (id = current_setting('app.current_client_id', true))
    WITH CHECK (id = current_setting('app.current_client_id', true));

-- kpi_definitions: a client's KPIs, targets and owner contacts stay private to that client.
CREATE POLICY client_isolation ON kpi_definitions
    USING (client_id = current_setting('app.current_client_id', true))
    WITH CHECK (client_id = current_setting('app.current_client_id', true));

-- source_facts: raw financials (revenue by customer, COGS, cash, AR) are per-client only.
CREATE POLICY client_isolation ON source_facts
    USING (client_id = current_setting('app.current_client_id', true))
    WITH CHECK (client_id = current_setting('app.current_client_id', true));

-- kpi_values: computed KPIs and drivers can only be read or written for the current client.
CREATE POLICY client_isolation ON kpi_values
    USING (client_id = current_setting('app.current_client_id', true))
    WITH CHECK (client_id = current_setting('app.current_client_id', true));

-- weekly_runs: each client's runs and packets are isolated; the two run-child
-- policies below build on this one.
CREATE POLICY client_isolation ON weekly_runs
    USING (client_id = current_setting('app.current_client_id', true))
    WITH CHECK (client_id = current_setting('app.current_client_id', true));

-- doc_chunks: vector search only ever ranks the current client's documents,
-- so retrieval can't surface another client's past explanations.
CREATE POLICY client_isolation ON doc_chunks
    USING (client_id = current_setting('app.current_client_id', true))
    WITH CHECK (client_id = current_setting('app.current_client_id', true));

-- owner_responses: no client_id column; a reply is visible (or insertable) only
-- if its parent run belongs to the current client.
CREATE POLICY client_isolation ON owner_responses
    USING (EXISTS (
        SELECT 1 FROM weekly_runs r
        WHERE r.id = owner_responses.run_id
          AND r.client_id = current_setting('app.current_client_id', true)))
    WITH CHECK (EXISTS (
        SELECT 1 FROM weekly_runs r
        WHERE r.id = owner_responses.run_id
          AND r.client_id = current_setting('app.current_client_id', true)));

-- approvals: same rule as owner_responses, so a Controller decision can't be
-- read or attached to another client's run.
CREATE POLICY client_isolation ON approvals
    USING (EXISTS (
        SELECT 1 FROM weekly_runs r
        WHERE r.id = approvals.run_id
          AND r.client_id = current_setting('app.current_client_id', true)))
    WITH CHECK (EXISTS (
        SELECT 1 FROM weekly_runs r
        WHERE r.id = approvals.run_id
          AND r.client_id = current_setting('app.current_client_id', true)));
