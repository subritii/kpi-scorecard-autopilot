# KPI Scorecard Autopilot

Prototype of a weekly KPI scorecard automation for a multi-client fractional accounting/CFO firm.
Every Monday it pulls each client's numbers, computes KPIs, flags red ones, asks each KPI owner
"what changed / why / who owns it / what's being done", assembles a one-page packet, and sends it
only after a Controller approves.

This is a portfolio project. Synthetic data and sandboxes only.

## Non-negotiable rules

1. **KPI math is deterministic Python with unit tests.** Never compute or round numbers in an LLM prompt.
2. **The LLM describes, it does not diagnose.** It may say what moved and which drivers contributed. Causes come only from owner replies.
3. **Every SQL and vector query filters by `client_id`.** Client A must never see Client B's data.
4. **Nothing is sent to a client without an `approvals` row with decision = `approved`.**
5. **No real company names, logos, people, or financial data.** Fictional clients only.
6. **Secrets live in `.env` only.** Never commit them, never hardcode them, never print them in logs.
7. **The LLM provider is swappable** through `api/app/llm/` (`azure_openai` or `ollama`). No provider-specific calls outside that module.

## Stack

- **n8n** (self-hosted, Docker): schedules, integrations, waiting for human replies, routing
- **FastAPI** (Python 3.11+): KPI math, driver analysis, faithfulness check, eval runner
- **Postgres 16 + pgvector**: all data, embeddings, row-level security on `client_id`
- **LLM**: Azure OpenAI or local Ollama, chosen by `LLM_PROVIDER`
- **Integrations**: QuickBooks Online sandbox, ClickUp, Gmail (or Teams/Slack) for approvals
- **Tooling**: pytest, ruff, Docker Compose

## Repo layout

```
.
├── CLAUDE.md
├── README.md
├── docker-compose.yml        # n8n, postgres (pgvector), api
├── .env.example              # every variable, no real values
├── api/
│   ├── pyproject.toml
│   ├── app/
│   │   ├── main.py           # FastAPI app + routes
│   │   ├── db.py             # connection, sets app.current_client_id per request
│   │   ├── service.py        # SQL for the endpoints; Decimal -> string for JSON
│   │   ├── kpis.py           # pure KPI formulas + status logic (no I/O)
│   │   ├── drivers.py        # top contributors to a change
│   │   ├── faithfulness.py   # every number in text must exist in kpi_values
│   │   ├── retrieval.py      # pgvector search, always client-scoped
│   │   └── llm/
│   │       ├── base.py       # LLMProvider interface: complete(), embed()
│   │       ├── azure_openai.py
│   │       └── ollama.py
│   └── tests/
├── db/
│   └── migrations/           # numbered SQL files, applied in order
├── data/
│   └── seed/                 # generated synthetic CSV/JSON (gitignored if large)
├── scripts/
│   ├── generate_synthetic.py # 2 clients x 12 weeks with planted events
│   ├── seed_db.py
│   ├── embed_docs.py         # KPI definitions, notes, past explanations -> doc_chunks
│   └── run_eval.py
├── n8n/
│   └── workflows/            # exported workflow JSON (WF1..WF6)
├── prompts/                  # LLM prompt templates as .md files
└── eval/
    └── results.md            # latest eval output
```

## Data model

| Table | Key columns |
|---|---|
| `clients` | id, name, controller_email, leadership_emails, clickup_list_id |
| `kpi_definitions` | id, client_id, name, definition, owner_name, owner_email, source, formula_key, target, direction (`higher`/`lower`), yellow_band_pct |
| `source_facts` | client_id, week_start, metric_key, value, detail_json |
| `source_loads` | client_id, week_start, source, row_count, loaded_at |
| `kpi_values` | client_id, kpi_id, week_start, value, prior_value, status, drivers_json |
| `weekly_runs` | id, client_id, week_start, state, packet_md, created_at |
| `owner_responses` | run_id, kpi_id, what_changed, why, owner_of_fix, actions, received_at |
| `approvals` | run_id, approver, decision, edits, decided_at |
| `doc_chunks` | id, client_id, kpi_id, week_start, source_type, text, embedding |

- `weekly_runs.state`: `collecting → asking → assembling → awaiting_approval → sent | rejected | needs_attention`
- `doc_chunks.source_type`: `definition | note | past_explanation`
- Row-level security: policies use `current_setting('app.current_client_id')`. `db.py` sets it on every connection checkout.
- Two DB roles: `kpi_admin` (superuser; migrations and seeding only, bypasses RLS) and `app_user` (API and tests only; RLS enforced, FORCE RLS on all tables). Never point the API at the admin URL.
- `owner_responses` and `approvals` have no `client_id`; their RLS policy inherits isolation from `weekly_runs`.
- A trigger blocks `weekly_runs.state = 'sent'` unless an approved `approvals` row exists (rule 4).
- `source_loads.source`: `qbo_profit_and_loss | qbo_balance_sheet | qbo_ar_aging | sales_sheet`. A KPI whose report wasn't loaded that week is `no_data`; a loaded report with no rows for a metric means 0.
- Embedding dimension comes from `EMBEDDING_DIM` in `.env`. Do not hardcode it.

## KPI formulas (`formula_key`)

- `weekly_revenue`: sum of `revenue` facts for the week
- `gross_margin_pct`: (revenue − cogs) / revenue × 100; return None if revenue = 0. Kept unrounded for status; shown to 1 decimal
- `cash_on_hand`: ending cash balance for the week
- `ar_over_60`: AR aging buckets 61–90 + 90+
- `new_bookings`: sum of bookings from the sales sheet
- `inventory_days`: inventory balance / (weekly cogs / 7)

**Status logic**
- green: meets target in the KPI's direction
- yellow: misses target by no more than `yellow_band_pct` (default 5%)
- red: misses by more than that

## Synthetic clients (fictional)

**Brightline Services**: ~$8M services company
KPIs: weekly_revenue ≥ $155K, gross_margin_pct ≥ 42, cash_on_hand ≥ $400K, ar_over_60 ≤ $90K, new_bookings ≥ $170K

**Northgate Supply**: ~$12M distributor
KPIs: weekly_revenue ≥ $230K, gross_margin_pct ≥ 28, cash_on_hand ≥ $600K, ar_over_60 ≤ $150K, inventory_days ≤ 45

12 weeks each, weeks start Monday 2026-07-06. Use a fixed random seed so data is reproducible.

**Planted events (ground truth for evals; store in `data/seed/ground_truth.json`)**
- Brightline weeks 5–7: two customers pay late → ar_over_60 red; week 8 yellow as one pays in full and the other partly; week 9 green. Top drivers are those two customers
- Brightline week 8: supplier price increase → gross_margin_pct red; top driver is one COGS account (Subcontractor fees)
- Brightline weeks 10–11: seasonal dip → new_bookings yellow then red; ~75% of the dip is in Implementation, the expected top driver
- Northgate week 6: overstock purchase → inventory_days red (~55); week 7 still red (~50), week 8 yellow (~46.5), week 9 green. Top driver is Plumbing fixtures
- All other weeks: normal noise, should stay green (110 green, 7 red, 3 yellow in total)
- `ground_truth.json` lists `expected_statuses` for all 120 client × week × KPI values, plus `events` with expected top drivers
- False alarm = a computed status other than green where `expected_status` is green
- Include a few `past_explanation` docs from "last year" for the same patterns so retrieval has history

## API endpoints

- `POST /compute` `{client_id, week_start}` → KPI values + status, writes `kpi_values`
- `POST /drivers` `{client_id, kpi_id, week_start}` → ranked contributors with amounts
- `POST /retrieve` `{client_id, kpi_id, week_start, query}` → past explanations of that KPI dated before `week_start`, above `RETRIEVAL_MIN_SIMILARITY` (client-scoped; empty if nothing is close enough)
- `POST /check-faithfulness` `{run_id, text}` → pass/fail + list of unmatched numbers
- `POST /eval/run` → metrics dict, also writes `eval/results.md`
- `GET /health`

## n8n workflows

- **WF1 Collect & compute** (Mon 6:00): pull sources → `/compute` → create `weekly_runs` row + ClickUp task
- **WF2 Investigate & ask**: per red KPI → `/drivers` + `/retrieve` → LLM observation → send-and-wait to owner → save `owner_responses`
- **WF3 Reminder** (Tue 9:00): nudge owners with no reply
- **WF4 Assemble & approve** (Tue 12:00): build packet → `/check-faithfulness` → Controller approve/reject → on approve: email leadership, comment + close ClickUp task
- **WF5 Remember**: on approval, embed owner explanations into `doc_chunks`
- **WF6 Error handler**: any failure → alert + set run state `needs_attention`

Workflows are built in the n8n editor and exported to `n8n/workflows/`. Do not rewrite exported JSON unless asked. When reviewing it, check for hardcoded secrets, missing error branches, and queries without `client_id`.

## Evaluation targets

| Metric | Target |
|---|---|
| KPI math accuracy vs. hand-computed | 100% |
| Planted events detected | all |
| False alarms across 24 client-weeks | ≤ 1 |
| Top driver = planted cause | ≥ 90% |
| Narrative numbers match data | 100% after check |
| Retrieved explanation relevant | ≥ 80% |
| Cross-client leaks | 0 |

## Commands

```
cp .env.example .env               # then fill in values
docker compose up -d               # n8n :5678, api :8000, postgres :5432
cd api && pip install -e ".[dev]"
pytest                             # run all tests (live tests skipped)
pytest --live -s                   # also run tests against real Ollama, printing scores
ruff check . && ruff format .
python scripts/generate_synthetic.py
python scripts/migrate.py          # as admin; applies db/migrations, sets app_user password
python scripts/seed_db.py          # as admin; upserts data/seed/ in one transaction
python scripts/embed_docs.py       # as admin; re-runnable (upserts on doc_key)
python scripts/run_eval.py
```

## How to work in this repo

- Work one checklist item at a time. Keep changes small and focused.
- For KPI math, drivers, and faithfulness: write tests first, then code.
- Keep `kpis.py` pure (no database or network calls) so it is easy to test.
- Prompts live in `prompts/` as files, not inline strings.
- After each change: run `pytest` and `ruff`. Do not say a step is done if tests fail.
- Explain non-obvious code with short comments. I need to be able to walk through every file in an interview.
- Ask before adding a new dependency or service.
- Never run destructive database commands (DROP, TRUNCATE, deleting volumes) without asking.

## Build phases (update the checkbox as we go)

- [x] **Phase 0 Setup**: docker-compose, .env.example, pyproject, health endpoint, README stub
- [x] **Phase 1 Data**: migrations + RLS, synthetic generator + ground truth, seed script
- [x] **Phase 2 Math**: `/compute` and `/drivers` with tests passing
- [ ] **Phase 3 RAG**: LLM provider interface, embed_docs, `/retrieve` (client-scoped), isolation test
- [ ] **Phase 4 Guardrails**: `/check-faithfulness` with tests
- [ ] **Phase 5 Workflows**: WF1–WF6 in n8n, exported to repo
- [ ] **Phase 6 Integrations**: one live QuickBooks sandbox report pull, ClickUp task create/comment
- [ ] **Phase 7 Eval + ship**: `/eval/run`, `eval/results.md`, README with diagram and results

Current phase: **Phase 3**