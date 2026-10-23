-- A person's decision about a stuck send claim (claimed, but never marked sent):
-- "sent" (the packet did go out) or "not_sent" (release the claim so it can be
-- sent again). Append-only: who decided, when, and which claim it was.

CREATE TABLE claim_resolutions (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id      integer NOT NULL REFERENCES weekly_runs (id),
    resolution  text NOT NULL CHECK (resolution IN ('sent', 'not_sent')),
    resolved_by text NOT NULL,
    note        text,
    claim_key   text NOT NULL,  -- the claim being resolved
    resolved_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

GRANT SELECT, INSERT ON claim_resolutions TO app_user;
ALTER TABLE claim_resolutions ENABLE ROW LEVEL SECURITY;
ALTER TABLE claim_resolutions FORCE ROW LEVEL SECURITY;

-- claim_resolutions: visible (or insertable) only if its parent run belongs to
-- the current client.
CREATE POLICY client_isolation ON claim_resolutions
    USING (EXISTS (
        SELECT 1 FROM weekly_runs r
        WHERE r.id = claim_resolutions.run_id
          AND r.client_id = current_setting('app.current_client_id', true)))
    WITH CHECK (EXISTS (
        SELECT 1 FROM weekly_runs r
        WHERE r.id = claim_resolutions.run_id
          AND r.client_id = current_setting('app.current_client_id', true)));
