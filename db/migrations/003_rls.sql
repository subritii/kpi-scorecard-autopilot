-- Row-level security: a connection only sees rows for the client named in
-- the session setting app.current_client_id (set by api/app/db.py).
--
-- Superusers bypass RLS, so the API must NOT connect as the admin user.
-- It connects as kpi_app, an ordinary role. The role is created NOLOGIN here;
-- scripts/migrate.py enables login and sets its password from .env.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'kpi_app') THEN
        CREATE ROLE kpi_app NOLOGIN;
    END IF;
END
$$;

-- Least privilege: no DELETE anywhere; approvals and owner responses are append-only.
GRANT USAGE ON SCHEMA public TO kpi_app;
GRANT SELECT, INSERT, UPDATE ON
    clients, kpi_definitions, source_facts, kpi_values, weekly_runs, doc_chunks
    TO kpi_app;
GRANT SELECT, INSERT ON owner_responses, approvals TO kpi_app;

-- Returns NULL when the setting is missing or empty, so an unscoped
-- connection matches no rows (fails closed instead of erroring or leaking).
CREATE FUNCTION current_client_id() RETURNS text
LANGUAGE sql STABLE AS $$
    SELECT NULLIF(current_setting('app.current_client_id', true), '')
$$;

ALTER TABLE clients         ENABLE ROW LEVEL SECURITY;
ALTER TABLE kpi_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_facts    ENABLE ROW LEVEL SECURITY;
ALTER TABLE kpi_values      ENABLE ROW LEVEL SECURITY;
ALTER TABLE weekly_runs     ENABLE ROW LEVEL SECURITY;
ALTER TABLE owner_responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals       ENABLE ROW LEVEL SECURITY;
ALTER TABLE doc_chunks      ENABLE ROW LEVEL SECURITY;

-- USING filters what you can read/update; WITH CHECK blocks writing rows
-- for any other client.
CREATE POLICY client_isolation ON clients
    USING (id = current_client_id()) WITH CHECK (id = current_client_id());
CREATE POLICY client_isolation ON kpi_definitions
    USING (client_id = current_client_id()) WITH CHECK (client_id = current_client_id());
CREATE POLICY client_isolation ON source_facts
    USING (client_id = current_client_id()) WITH CHECK (client_id = current_client_id());
CREATE POLICY client_isolation ON kpi_values
    USING (client_id = current_client_id()) WITH CHECK (client_id = current_client_id());
CREATE POLICY client_isolation ON weekly_runs
    USING (client_id = current_client_id()) WITH CHECK (client_id = current_client_id());
CREATE POLICY client_isolation ON doc_chunks
    USING (client_id = current_client_id()) WITH CHECK (client_id = current_client_id());

-- These tables have no client_id; a row is visible only if its run is.
-- The subquery on weekly_runs is itself filtered by weekly_runs' policy.
CREATE POLICY client_isolation ON owner_responses
    USING (EXISTS (SELECT 1 FROM weekly_runs r WHERE r.id = run_id))
    WITH CHECK (EXISTS (SELECT 1 FROM weekly_runs r WHERE r.id = run_id));
CREATE POLICY client_isolation ON approvals
    USING (EXISTS (SELECT 1 FROM weekly_runs r WHERE r.id = run_id))
    WITH CHECK (EXISTS (SELECT 1 FROM weekly_runs r WHERE r.id = run_id));
