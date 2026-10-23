-- Claim the send before emailing, so an n8n retry can't email leadership twice.
--
-- POST /runs/{id}/claim-send records who is sending (an idempotency key, e.g.
-- the n8n execution id) for one approved packet version. Only that key can
-- then mark the run sent. A retry with the same key gets the same claim; a
-- different key is refused. Columns only, so the state CHECK is unchanged.

ALTER TABLE weekly_runs
    ADD COLUMN send_claim_key text,
    ADD COLUMN send_claimed_at timestamptz,
    ADD COLUMN send_claim_version integer;
