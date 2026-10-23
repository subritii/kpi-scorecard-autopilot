"""API key auth: every route needs X-API-Key, compared in constant time."""

import os
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth
from app.main import api, app

GOOD = os.environ["API_KEY"]  # set by conftest (from .env, or a test key)
# The only routes without the API key: the development-only docs pages (off by
# default), and the pages people open from an email, which authenticate with the
# signed, single-use token in the link instead.
DOCS_PATHS = {"/docs", "/openapi.json"}
TOKEN_PATHS = {"/approve", "/reply"}
anonymous = TestClient(app)


def every_route():
    # The API's routes live on the key-protected `api` router. (Walking
    # app.routes would miss them: newer FastAPI includes routers lazily.)
    for route in api.routes:
        if isinstance(route, APIRoute):
            path = re.sub(r"\{[^}]+\}", "1", route.path)  # fill path params
            for method in sorted(route.methods):
                yield pytest.param(method, path, id=f"{method} {route.path}")


@pytest.mark.parametrize("method, path", list(every_route()))
def test_every_route_rejects_a_missing_key(method, path):
    resp = anonymous.request(method, path, json={})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "missing or invalid API key"}


@pytest.mark.parametrize("method, path", list(every_route()))
def test_every_route_rejects_a_wrong_key(method, path):
    resp = anonymous.request(method, path, json={}, headers={"X-API-Key": GOOD + "x"})
    assert resp.status_code == 401


def test_routes_exist_to_check():
    # Guards against the parametrized tests above silently collecting nothing.
    assert len(list(every_route())) >= 10


def test_only_docs_and_token_routes_are_outside_the_protected_router():
    unprotected = {r.path for r in app.routes if isinstance(r, APIRoute)}
    assert unprotected == DOCS_PATHS | TOKEN_PATHS


@pytest.mark.parametrize("path", sorted(TOKEN_PATHS))
def test_token_routes_refuse_requests_without_a_valid_token(path):
    # No API key and no token: refused, never served.
    assert anonymous.get(path).status_code == 403
    assert anonymous.post(path, data={}).status_code in (403, 422)


def test_right_key_is_accepted():
    assert anonymous.get("/health", headers={"X-API-Key": GOOD}).json() == {"status": "ok"}


def test_missing_and_wrong_key_look_the_same():
    missing = anonymous.get("/health")
    wrong = anonymous.get("/health", headers={"X-API-Key": "nope"})
    assert (missing.status_code, missing.json()) == (wrong.status_code, wrong.json())


def test_comparison_is_constant_time(monkeypatch):
    calls = []

    def spy(a, b):
        calls.append((type(a), type(b)))
        return a == b

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    anonymous.get("/health", headers={"X-API-Key": GOOD})
    assert calls == [(bytes, bytes)]


@pytest.mark.parametrize("configured", [None, "", "too-short"])
def test_unset_or_weak_server_key_fails_closed(monkeypatch, configured):
    if configured is None:
        monkeypatch.delenv("API_KEY", raising=False)
    else:
        monkeypatch.setenv("API_KEY", configured)
    # Even a request that sends exactly the configured value is refused.
    resp = anonymous.get("/health", headers={"X-API-Key": configured or ""})
    assert resp.status_code == 500
    assert "API_KEY" in resp.json()["detail"]
    if configured:
        assert configured not in resp.json()["detail"]  # never echoes the value


def test_health_needs_the_key_too():
    # One rule, no exceptions: health checks send the key like everything else.
    assert anonymous.get("/health").status_code == 401


@pytest.mark.parametrize("setting", [None, "", "false", "0", "no"])
def test_docs_are_off_by_default(monkeypatch, setting):
    if setting is None:
        monkeypatch.delenv("DOCS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("DOCS_ENABLED", setting)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert anonymous.get(path).status_code == 404


@pytest.mark.parametrize("setting", ["true", "1", "yes", "TRUE"])
def test_docs_can_be_turned_on_for_development(monkeypatch, setting):
    monkeypatch.setenv("DOCS_ENABLED", setting)
    assert anonymous.get("/docs").status_code == 200  # a browser can open it
    schema = anonymous.get("/openapi.json").json()
    assert "/runs/start" in schema["paths"]
    assert "/docs" not in schema["paths"]  # the docs routes aren't part of the API
    # The API itself still needs the key.
    assert anonymous.get("/health").status_code == 401


# --- startup ---------------------------------------------------------------------


@pytest.mark.parametrize("configured", [None, "", "too-short"])
def test_api_refuses_to_start_without_a_strong_key(monkeypatch, configured):
    if configured is None:
        monkeypatch.delenv("API_KEY", raising=False)
    else:
        monkeypatch.setenv("API_KEY", configured)
    # Entering TestClient as a context manager runs the app's startup.
    with pytest.raises(RuntimeError, match="API_KEY"):
        with TestClient(app):
            pass


def test_api_starts_with_a_strong_key():
    with TestClient(app) as started:
        assert started.get("/health", headers={"X-API-Key": GOOD}).status_code == 200


# --- docker-compose -----------------------------------------------------------------


def test_compose_ports_bind_to_localhost_only():
    from app.config import REPO_ROOT

    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    ports = re.findall(r'^\s*- "([^"]+)"\s*$', compose, re.M)
    published = [p for p in ports if re.fullmatch(r"[\d.]*:?\d+:\d+", p)]
    assert len(published) == 3, published  # postgres, api, n8n
    assert all(p.startswith("127.0.0.1:") for p in published), published
