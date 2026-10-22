# KPI Scorecard Autopilot

Weekly KPI scorecard automation for a multi-client fractional accounting/CFO firm.
Every Monday it pulls each client's numbers, computes KPIs, flags red ones, asks each KPI
owner what changed and why, assembles a one-page packet, and sends it only after a
Controller approves.

> Portfolio project. All clients, people, and numbers are synthetic.

## Stack

n8n (orchestration) · FastAPI (KPI math, drivers, guardrails, evals) · Postgres 16 + pgvector ·
Azure OpenAI or Ollama (swappable) · QuickBooks Online sandbox · ClickUp · Gmail

## Quick start

```bash
cp .env.example .env               # then fill in values
docker compose up -d               # n8n :5678, api :8000, postgres :5432
curl localhost:8000/health         # {"status":"ok"}
```

Load the synthetic data (with the API venv active, from the repo root):

```bash
python scripts/generate_synthetic.py   # data/seed/, fixed seed, reproducible
python scripts/migrate.py              # schema + row-level security (runs as admin)
python scripts/seed_db.py              # loads data (runs as admin)
```

Local development of the API:

```bash
cd api
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check . && ruff format .
```

## Status

- [x] Phase 0 Setup
- [x] Phase 1 Data
- [x] Phase 2 Math
- [ ] Phase 3 RAG
- [ ] Phase 4 Guardrails
- [ ] Phase 5 Workflows
- [ ] Phase 6 Integrations
- [ ] Phase 7 Eval + ship

Architecture diagram and eval results will be added in Phase 7.
