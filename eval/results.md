# Evaluation results

Run on Oct 23, 2026 with `python scripts/run_eval.py`.
Synthetic data: 2 clients x 12 weeks (120 client-week-KPI values), with the
planted events in `data/seed/ground_truth.json`. Models: chat `llama3.2:3b`,
embeddings `ollama/nomic-embed-text` (Ollama, local).

| Metric | Target | Result | |
|---|---|---|---|
| KPI math accuracy vs. hand-computed | 100% | 100% (120/120) | pass |
| KPI status vs. ground truth | 100% | 100% (120/120) | pass |
| Planted events detected | all | 4/4 | pass |
| False alarms across 24 client-weeks | ≤ 1 | 0 | pass |
| Top driver = planted cause | ≥ 90% | 100% (10/10 event weeks) | pass |
| Narrative numbers match data (after check) | 100% | 100% (7/7 observations) | pass |
| Packets pass the faithfulness check | 100% | 7/7 | pass |
| Retrieved explanation relevant | ≥ 80% | 100% (4/4 planted events) | pass |
| Query with no history returns nothing | 0 results | 0 | pass |
| Cross-client leaks | 0 | 0 rows in 12 tables; 0 names in packets | pass |

## Details

- **KPI math** is checked against a second, written-out implementation of each
  formula in the eval script (exact Decimal arithmetic, compared to 1e-9).
- **False alarms** are non-green statuses where the ground truth says green;
  **misses** (planted weeks not flagged): 0.
- **Drivers**: the first-ranked contributor is a planted cause in
  10 of 10 event weeks (spikes and wind-downs).
- **LLM observations** (`llama3.2:3b`, temperature 0): 7 of
  7 passed the faithfulness check on the first try. Final
  sources: 7 llm. A failing draft gets one retry, then falls back to a
  template built only from computed numbers, so what is sent always passes.
  Timing (110.0s per run here) depends heavily on the machine and on what else
  is running: this run happened while the laptop was overloaded (load average
  around 490); an earlier run on an idle machine took about 3s per model call.
  It is not a benchmark.
- **Packets** are built without owner replies (nobody answers during an
  automated run), so they exercise the "No reply yet" path; packets with
  verbatim owner quotes are covered by the unit tests.
- **Retrieval**: threshold `RETRIEVAL_MIN_SIMILARITY=0.73`. Lowest
  correct match 0.7685, highest wrong match
  0.6877 (gap 0.0808).
  Tuned on this small synthetic set; see the README.
- **Leaks**: as each client, every visible row in all 12
  client-owned tables belongs to that client (row-level security), and no
  packet names another client's customers or accounts.
