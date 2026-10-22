-- app_user: the only role the API, seed script and tests connect as.
--
-- Two kinds of roles skip RLS: superusers (always) and a table's owner
-- (unless FORCE is set). The admin user from POSTGRES_USER is both, so it is
-- used for migrations only. app_user is neither: not a superuser, no
-- BYPASSRLS, owns no tables. It is created NOLOGIN here; scripts/migrate.py
-- turns on login and sets its password from APP_DB_PASSWORD in .env.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

-- Least privilege: read/write only what the app needs. No DELETE anywhere, and
-- owner replies and approvals can only be added to, never edited.
-- Identity columns need no separate sequence grants.
GRANT USAGE ON SCHEMA public TO app_user;
GRANT SELECT, INSERT, UPDATE ON
    clients, kpi_definitions, source_facts, kpi_values, weekly_runs, doc_chunks
    TO app_user;
GRANT SELECT, INSERT ON owner_responses, approvals TO app_user;

-- FORCE applies the policies to the table owner too, so a non-superuser owner
-- can't quietly bypass them. (Superusers still bypass, which is why the API
-- never uses the admin user.)
ALTER TABLE clients         FORCE ROW LEVEL SECURITY;
ALTER TABLE kpi_definitions FORCE ROW LEVEL SECURITY;
ALTER TABLE source_facts    FORCE ROW LEVEL SECURITY;
ALTER TABLE kpi_values      FORCE ROW LEVEL SECURITY;
ALTER TABLE weekly_runs     FORCE ROW LEVEL SECURITY;
ALTER TABLE owner_responses FORCE ROW LEVEL SECURITY;
ALTER TABLE approvals       FORCE ROW LEVEL SECURITY;
ALTER TABLE doc_chunks      FORCE ROW LEVEL SECURITY;
