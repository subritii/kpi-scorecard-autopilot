-- The QuickBooks Online company (realm id) a client's data is pulled from.
-- Only a client with one can receive QuickBooks reports (POST /ingest/...), and
-- only from that company. Synthetic clients have none, so they stay synthetic.
ALTER TABLE clients ADD COLUMN qbo_realm_id text UNIQUE;
