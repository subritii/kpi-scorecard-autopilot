-- Which source reports were loaded for each client-week.
--
-- Lets KPI math tell "report loaded, nothing in it" (e.g. an AR aging report
-- with no 61+ rows -> 0, green) from "report never arrived" (-> no_data).
-- One row per report, written whenever that report is pulled, even if it had
-- zero rows.

CREATE TABLE source_loads (
    client_id  text NOT NULL REFERENCES clients (id),
    week_start date NOT NULL CHECK (EXTRACT(ISODOW FROM week_start) = 1),
    source     text NOT NULL CHECK (source IN (
                   'qbo_profit_and_loss', 'qbo_balance_sheet', 'qbo_ar_aging', 'sales_sheet')),
    row_count  integer NOT NULL CHECK (row_count >= 0),  -- 0 = loaded but empty
    loaded_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, week_start, source)
);

GRANT SELECT, INSERT, UPDATE ON source_loads TO app_user;

ALTER TABLE source_loads ENABLE ROW LEVEL SECURITY;
ALTER TABLE source_loads FORCE ROW LEVEL SECURITY;

-- source_loads: a client can only see or record loads for its own weeks.
CREATE POLICY client_isolation ON source_loads
    USING (client_id = current_setting('app.current_client_id', true))
    WITH CHECK (client_id = current_setting('app.current_client_id', true));
