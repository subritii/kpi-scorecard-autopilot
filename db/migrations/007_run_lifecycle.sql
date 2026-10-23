-- Run lifecycle support for the /runs endpoints.

-- 1. Audit log: every state change of a weekly run, with an optional note
--    (e.g. why a run needs attention). Insert-only for the app.
CREATE TABLE run_events (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     integer NOT NULL REFERENCES weekly_runs (id),
    from_state text,  -- NULL for the event that created the run
    to_state   text NOT NULL,
    note       text,
    created_at timestamptz NOT NULL DEFAULT now()
);

GRANT SELECT, INSERT ON run_events TO app_user;
ALTER TABLE run_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_events FORCE ROW LEVEL SECURITY;

-- run_events: no client_id column; an event is visible (or insertable) only
-- if its parent run belongs to the current client.
CREATE POLICY client_isolation ON run_events
    USING (EXISTS (
        SELECT 1 FROM weekly_runs r
        WHERE r.id = run_events.run_id
          AND r.client_id = current_setting('app.current_client_id', true)))
    WITH CHECK (EXISTS (
        SELECT 1 FROM weekly_runs r
        WHERE r.id = run_events.run_id
          AND r.client_id = current_setting('app.current_client_id', true)));

-- 2. An approval is for one version of the packet. Rebuilding the packet bumps
--    the version, so an earlier approval no longer allows sending.
ALTER TABLE weekly_runs ADD COLUMN packet_version integer NOT NULL DEFAULT 0;
ALTER TABLE approvals ADD COLUMN packet_version integer;

CREATE OR REPLACE FUNCTION require_approval_before_send() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'sent' AND NOT EXISTS (
        SELECT 1 FROM approvals
        WHERE run_id = NEW.id
          AND decision = 'approved'
          AND packet_version = NEW.packet_version
    ) THEN
        RAISE EXCEPTION
            'run % cannot be marked sent without an approval of packet version %',
            NEW.id, NEW.packet_version;
    END IF;
    RETURN NEW;
END;
$$;
