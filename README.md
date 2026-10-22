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
git config core.hooksPath .githooks   # once per clone: blocks commits unless ruff + pytest pass
                                      # (Postgres must be up: database tests may not skip)
cd api
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check . && ruff format .
```

## Retrieval threshold

`/retrieve` only returns a past explanation when its cosine similarity clears
`RETRIEVAL_MIN_SIMILARITY`, so the packet never claims "this happened before" without
evidence. With `nomic-embed-text` it is set to **0.73**, the middle of the gap between:

| | similarity |
|---|---|
| lowest correct match (planted event → last year's explanation of the same pattern) | 0.7685 |
| highest wrong match (other-KPI explanations, cross-client distractor, no-history query) | 0.6877 |

**This is tuned on a small test set:** 4 correct pairs and 17 wrong ones from the
synthetic data. Treat it as a starting point, re-measure with `python scripts/tune_retrieval.py`
(prints both groups and a suggested value) when the embedding model or the documents change,
and expect to revisit it with real history.

## Faithfulness check

Before a packet goes to the Controller, `/check-faithfulness` checks every AI-written
sentence against the run's data: numbers must be values of the KPI they describe (current
vs. prior wording included), names must be real contributors, dates must be the run's
week, directions must match the change, and causal language ("because", "due to") is
flagged, since only KPI owners explain causes. Owner replies are quoted verbatim and
exempt, but only if they match the stored reply exactly.

**The checker is a safety net, not the control.** It catches common LLM mistakes
automatically; the Controller's approval is the final control, and nothing is sent
without an `approved` approval (enforced in the database).

## Known limitations

- **Lowercase invented names aren't detected.** Names are found by capitalization, so a
  made-up "marketing spend" account slips through; "Marketing Spend" would be flagged.
- **Spelled-out numbers aren't checked.** "two customers" is not verified; "2 customers"
  is, against the real counts (contributors listed, red KPIs, consecutive red weeks).
- **The checks are heuristics tuned to the packet's style.** They fail closed (when
  unsure, they flag), which can mean false alarms on unusual phrasing.
- **The retrieval threshold is tuned on a small synthetic set** (see above).

## Status

- [x] Phase 0 Setup
- [x] Phase 1 Data
- [x] Phase 2 Math
- [x] Phase 3 RAG
- [ ] Phase 4 Guardrails
- [ ] Phase 5 Workflows
- [ ] Phase 6 Integrations
- [ ] Phase 7 Eval + ship

Architecture diagram and eval results will be added in Phase 7.
