"""web/server.py's "/" route: its own auth gate, and the carve-out that lets
a sign-out or failed sign-in actually render instead of bouncing straight
back through /oauth/login (see web/auth.py's logout() for why that loop
happens — Hack Club Auth keeps its own SSO session coolton can't end)."""

import pytest
from fastapi.testclient import TestClient

from web import auth
from web.server import app


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("COOLTON_WEB_SECRET", "test-secret")


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


def test_root_redirects_to_login_when_signed_out(client):
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/oauth/login"


def test_root_serves_the_app_when_signed_in(client):
    token = auth.create_session_token("U1")
    client.cookies.set(auth.SESSION_COOKIE, token)
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"<html" in resp.content.lower()


def test_root_serves_the_app_for_signed_out_param_even_without_a_session(client):
    """The actual bug: without this carve-out, landing on /?signed_out=1 with
    no session cookie hits the same "not authed" branch as any other visit
    and redirects to /oauth/login before the frontend ever shows anything."""
    resp = client.get("/?signed_out=1")
    assert resp.status_code == 200
    assert b"<html" in resp.content.lower()


def test_root_serves_the_app_for_auth_error_param_even_without_a_session(client):
    resp = client.get("/?auth_error=exchange")
    assert resp.status_code == 200
