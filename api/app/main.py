"""FastAPI app for KPI Scorecard Autopilot.

n8n calls these endpoints; all KPI math lives here in Python, never in an LLM prompt.
"""

from fastapi import FastAPI

app = FastAPI(title="KPI Scorecard Autopilot API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    # Liveness only: confirms the process is up. Database checks come in Phase 1.
    return {"status": "ok"}
