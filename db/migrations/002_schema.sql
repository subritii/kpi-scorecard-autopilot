-- Core tables. Every client-owned row carries client_id so row-level security
-- (003_rls.sql) can isolate clients from each other.

CREATE TABLE clients (
    id                text PRIMARY KEY,          -- readable slug, e.g. 'brightline'
    name              text NOT NULL,
    controller_email  text NOT NULL,
    leadership_emails text[] NOT NULL DEFAULT '{}',
    clickup_list_id   text
);

CREATE TABLE kpi_definitions (
    id              integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id       text NOT NULL REFERENCES clients (id),
    name            text NOT NULL,
    definition      text NOT NULL,
    owner_name      text NOT NULL,
    owner_email     text NOT NULL,
    source          text NOT NULL,
    formula_key     text NOT NULL CHECK (formula_key IN (
                        'weekly_revenue', 'gross_margin_pct', 'cash_on_hand',
                        'ar_over_60', 'new_bookings', 'inventory_days')),
    target          numeric NOT NULL,
    direction       text NOT NULL CHECK (direction IN ('higher', 'lower')),
    yellow_band_pct numeric NOT NULL DEFAULT 5,
    UNIQUE (client_id, formula_key),
    -- Lets child tables reference (kpi_id, client_id) together, so a row can
    -- never point at another client's KPI.
    UNIQUE (id, client_id)
);

-- Raw numbers from source systems, one row per metric per dimension
-- (e.g. revenue per customer, COGS per account). detail_json holds the
-- dimension, which is what driver analysis groups by.
CREATE TABLE source_facts (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id   text NOT NULL REFERENCES clients (id),
    week_start  date NOT NULL CHECK (EXTRACT(ISODOW FROM week_start) = 1),  -- Monday
    metric_key  text NOT NULL,
    value       numeric(14, 2) NOT NULL,
    detail_json jsonb NOT NULL DEFAULT '{}',
    UNIQUE (client_id, week_start, metric_key, detail_json)
);

CREATE TABLE kpi_values (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id    text NOT NULL,
    kpi_id       integer NOT NULL,
    week_start   date NOT NULL CHECK (EXTRACT(ISODOW FROM week_start) = 1),
    value        numeric,                        -- NULL when the formula is undefined
    prior_value  numeric,
    status       text NOT NULL CHECK (status IN ('green', 'yellow', 'red', 'no_data')),
    drivers_json jsonb,
    FOREIGN KEY (kpi_id, client_id) REFERENCES kpi_definitions (id, client_id),
    UNIQUE (client_id, kpi_id, week_start)
);

CREATE TABLE weekly_runs (
    id         integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id  text NOT NULL REFERENCES clients (id),
    week_start date NOT NULL CHECK (EXTRACT(ISODOW FROM week_start) = 1),
    state      text NOT NULL DEFAULT 'collecting' CHECK (state IN (
                   'collecting', 'asking', 'assembling', 'awaiting_approval',
                   'sent', 'rejected', 'needs_attention')),
    packet_md  text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (client_id, week_start)
);

-- owner_responses and approvals hang off a run; their client isolation comes
-- from weekly_runs (see 003_rls.sql).
CREATE TABLE owner_responses (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id       integer NOT NULL REFERENCES weekly_runs (id),
    kpi_id       integer NOT NULL REFERENCES kpi_definitions (id),
    what_changed text,
    why          text,
    owner_of_fix text,
    actions      text,
    received_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE approvals (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id     integer NOT NULL REFERENCES weekly_runs (id),
    approver   text NOT NULL,
    decision   text NOT NULL CHECK (decision IN ('approved', 'rejected')),
    edits      text,
    decided_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE doc_chunks (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    client_id   text NOT NULL REFERENCES clients (id),
    kpi_id      integer,                         -- NULL for client-wide notes
    week_start  date,
    source_type text NOT NULL CHECK (source_type IN ('definition', 'note', 'past_explanation')),
    text        text NOT NULL,
    -- {{EMBEDDING_DIM}} is replaced by scripts/migrate.py from .env.
    embedding   vector({{EMBEDDING_DIM}}),
    FOREIGN KEY (kpi_id, client_id) REFERENCES kpi_definitions (id, client_id)
);

CREATE INDEX doc_chunks_embedding_idx ON doc_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX doc_chunks_client_idx ON doc_chunks (client_id, kpi_id);

-- Rule 4 enforced in the database: a run can only reach state 'sent' if an
-- approvals row with decision = 'approved' exists for it.
CREATE FUNCTION require_approval_before_send() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'sent' AND NOT EXISTS (
        SELECT 1 FROM approvals WHERE run_id = NEW.id AND decision = 'approved'
    ) THEN
        RAISE EXCEPTION 'run % cannot be marked sent without an approved approval', NEW.id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER weekly_runs_require_approval
    BEFORE INSERT OR UPDATE OF state ON weekly_runs
    FOR EACH ROW EXECUTE FUNCTION require_approval_before_send();
