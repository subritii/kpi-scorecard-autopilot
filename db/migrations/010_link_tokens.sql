-- Signed links sent to people (the Controller's approval page, owner reply
-- forms). The link itself carries a signed token (app/links.py); this table
-- makes each one single-use and lets the server refuse it once used or expired.

CREATE TABLE link_tokens (
    jti            text PRIMARY KEY,  -- random id inside the signed token
    run_id         integer NOT NULL REFERENCES weekly_runs (id),
    purpose        text NOT NULL CHECK (purpose IN ('approve', 'reply')),
    kpi_id         integer REFERENCES kpi_definitions (id),  -- reply links only
    packet_version integer,                                 -- approve links only
    expires_at     timestamptz NOT NULL,
    used_at        timestamptz,                             -- set once, when used
    created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((purpose = 'reply') = (kpi_id IS NOT NULL)),
    CHECK ((purpose = 'approve') = (packet_version IS NOT NULL))
);

-- UPDATE is needed only to set used_at.
GRANT SELECT, INSERT, UPDATE ON link_tokens TO app_user;
ALTER TABLE link_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE link_tokens FORCE ROW LEVEL SECURITY;

-- link_tokens: visible (or insertable) only if its parent run belongs to the
-- current client.
CREATE POLICY client_isolation ON link_tokens
    USING (EXISTS (
        SELECT 1 FROM weekly_runs r
        WHERE r.id = link_tokens.run_id
          AND r.client_id = current_setting('app.current_client_id', true)))
    WITH CHECK (EXISTS (
        SELECT 1 FROM weekly_runs r
        WHERE r.id = link_tokens.run_id
          AND r.client_id = current_setting('app.current_client_id', true)));
