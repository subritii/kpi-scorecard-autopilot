import httpx
import pytest

from app.config import load_env

# Let tests find DATABASE_URL etc. from the repo's .env when run locally.
load_env()


@pytest.fixture(autouse=True)
def block_real_http(monkeypatch):
    """Tests must never call a real model: any real httpx network request fails.

    Providers under test use httpx.MockTransport instead; FastAPI's TestClient
    has its own in-process transport, so it is unaffected.
    """

    def refuse(self, request):
        raise RuntimeError(f"real network call blocked in tests: {request.method} {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
