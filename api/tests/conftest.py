import os

import httpx
import pytest

from app.config import load_env

# Let tests find DATABASE_URL etc. from the repo's .env when run locally.
load_env()
# Every endpoint needs an API key; tests use the .env one, or this fallback.
os.environ.setdefault("API_KEY", "test-api-key-" + "0" * 32)
os.environ.setdefault("LINK_SECRET", "test-link-secret-" + "1" * 32)
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
# Independent of your .env: tests never send email, so they run in live mode
# and check the real (.example) addresses; tests of test mode switch it on.
os.environ.pop("TEST_EMAIL_OVERRIDE", None)
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
