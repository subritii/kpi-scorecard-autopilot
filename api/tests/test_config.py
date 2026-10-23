"""The .env loader."""

import os

from app.config import load_env


def test_later_duplicate_wins_like_docker_compose(tmp_path, monkeypatch):
    # Docker Compose and python-dotenv use the last value; the loader must agree,
    # or the API container and host scripts would see different settings.
    monkeypatch.delenv("KPI_TEST_SETTING", raising=False)
    env = tmp_path / ".env"
    env.write_text("KPI_TEST_SETTING=\n# comment\nKPI_TEST_SETTING=second\n")
    load_env(env)
    assert os.environ["KPI_TEST_SETTING"] == "second"


def test_existing_environment_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("KPI_TEST_SETTING", "from-shell")
    env = tmp_path / ".env"
    env.write_text("KPI_TEST_SETTING=from-file\n")
    load_env(env)
    assert os.environ["KPI_TEST_SETTING"] == "from-shell"
