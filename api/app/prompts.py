"""LLM prompt templates live in prompts/ as Markdown files, not inline strings."""

from app.config import REPO_ROOT

PROMPTS_DIR = REPO_ROOT / "prompts"


def render(name: str, **values: str) -> str:
    """Fill a prompt file's {placeholders}. A missing value raises KeyError."""
    return (PROMPTS_DIR / name).read_text().format(**values)
