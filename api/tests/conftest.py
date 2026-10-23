import os
from urllib.parse import urlsplit

import httpx
import pytest

from app.config import load_env

# Let tests find settings from the repo's .env when run locally.
load_env()


def _db_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


# Tests use only the test database, never the dev/demo one: demo runs would
# break the tests, and tests must never touch demo data. Without TEST_ URLs the
# database tests skip, and the pre-commit hook then blocks the commit.
_test_app, _test_admin = (
    os.environ.get("TEST_DATABASE_URL"),
    os.environ.get("TEST_ADMIN_DATABASE_URL"),
)
for _url in (u for u in (_test_app, _test_admin) if u):
    if not _db_name(_url).endswith("_test"):
        raise RuntimeError(
            f"refusing to run tests against {_db_name(_url)!r}: not a *_test database"
        )
# Values are overwritten, never removed: a script's load_env() (called by some
# tests) only fills variables that are unset, so a removed one would come back
# from .env, pointing at the dev database.
_NO_TEST_DB = "postgresql://no-test-database-configured@127.0.0.1:1/none_test"
os.environ["DATABASE_URL"] = _test_app if _test_app and _test_admin else _NO_TEST_DB
os.environ["ADMIN_DATABASE_URL"] = _test_admin if _test_app and _test_admin else _NO_TEST_DB
# Every endpoint needs an API key; tests use the .env one, or this fallback.
os.environ.setdefault("API_KEY", "test-api-key-" + "0" * 32)
os.environ.setdefault("LINK_SECRET", "test-link-secret-" + "1" * 32)
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
# Independent of your .env: tests never send email, so they run in live mode
# and check the real (.example) addresses; tests of test mode switch it on.
# Empty rather than removed, for the same reason as the database URLs above.
os.environ["TEST_EMAIL_OVERRIDE"] = ""
os.environ["EMAIL_MODE"] = "live"


def pytest_addoption(parser):
    parser.addoption(
        "--live",
        action="store_true",
        help="also run tests marked 'live', which call the real Ollama models",
    )


def pytest_collection_modifyitems(config, items):
    """Skip 'live' tests unless --live was given."""
    if config.getoption("--live"):
        return
    skip = pytest.mark.skip(reason="uses real Ollama; run with: pytest --live")
    for item in items:
        # The marker, not item.keywords: keywords also contain parametrize ids,
        # so a test case with id "live" would be skipped by mistake.
        if item.get_closest_marker("live"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def block_real_http(request, monkeypatch):
    """Tests must never call a real model: any real httpx network request fails.

    Only tests marked 'live' (and run with --live) may reach the network.
    Providers under test use httpx.MockTransport instead; FastAPI's TestClient
    has its own in-process transport, so it is unaffected.
    """
    if request.node.get_closest_marker("live"):
        return

    def refuse(self, request):
        raise RuntimeError(f"real network call blocked in tests: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
