"""Minimal .env loader so scripts and tests can run outside Docker."""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_env(path: Path = REPO_ROOT / ".env") -> None:
    """Load KEY=VALUE lines into os.environ. Variables already set take precedence.

    If a key appears twice, the last one wins, as in Docker Compose and
    python-dotenv, so the API container and host scripts agree.
    """
    if not path.exists():
        return
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    for key, value in values.items():
        os.environ.setdefault(key, value)
