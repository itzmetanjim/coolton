import time
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import coolton_web_helper as web_helper


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret-token")
    monkeypatch.setattr(web_helper, "TOKEN_FILE", str(token_file))
    web_helper._ab_sessions.clear()
    yield
    web_helper._ab_sessions.clear()


@pytest.fixture
def client():
    return TestClient(web_helper.asgi_app)


def _auth():
    return {"Authorization": "Bearer secret-token"}


# ---------------------------------------------------------------------------
# /ab/register
# ---------------------------------------------------------------------------


def test_register_rejects_missing_bearer_token(client):
    resp = client.post("/ab/register", json={"upstream": "8848-sbx.e2b.app"})
    assert resp.status_code == 401


def test_register_rejects_wrong_bearer_token(client):
    resp = client.post(
        "/ab/register", headers={"Authorization": "Bearer wrong"}, json={"upstream": "8848-sbx.e2b.app"}
    )
    assert resp.status_code == 401


def test_register_rejects_a_non_e2b_upstream():
    client = TestClient(web_helper.asgi_app)
    resp = client.post("/ab/register", headers=_auth(), json={"upstream": "evil.example.com"})
    assert resp.status_code == 400


def test_register_accepts_a_valid_e2b_upstream_and_returns_a_public_url(client):
    resp = client.post("/ab/register", headers=_auth(), json={"upstream": "8848-sbx123.e2b.app"})
    assert resp.status_code == 200
    url = resp.json()["url"]
    assert url.startswith(f"{web_helper.BASE_URL}/ab/")


# ---------------------------------------------------------------------------
# /ab/{token}
# ---------------------------------------------------------------------------


def test_entry_sets_cookie_and_redirects_to_root(client):
    reg = client.post("/ab/register", headers=_auth(), json={"upstream": "8848-sbx.e2b.app"})
    token = reg.json()["url"].rsplit("/", 1)[-1]

    resp = client.get(f"/ab/{token}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert web_helper._AB_COOKIE in resp.cookies


def test_entry_404s_for_an_unknown_token(client):
    resp = client.get("/ab/does-not-exist")
    assert resp.status_code == 404


def test_entry_404s_for_an_expired_token(client):
    web_helper._ab_store("expired-tok", "8848-sbx.e2b.app")
    upstream, _ = web_helper._ab_sessions["expired-tok"]
    web_helper._ab_sessions["expired-tok"] = (upstream, time.time() - 1)
    resp = client.get("/ab/expired-tok")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# AgentBrowserProxy — HTTP
# ---------------------------------------------------------------------------


def test_root_path_with_valid_cookie_is_proxied_to_the_registered_upstream(client, monkeypatch):
    web_helper._ab_store("tok-ok", "8848-sbx.e2b.app")
    mock_response = httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>dashboard</html>")
    mock_request = AsyncMock(return_value=mock_response)
    monkeypatch.setattr(web_helper._ab_http_client, "request", mock_request)

    resp = client.get("/", headers={"Cookie": f"{web_helper._AB_COOKIE}=tok-ok"})
    assert resp.status_code == 200
    assert resp.content == b"<html>dashboard</html>"
    method, url = mock_request.call_args.args
    assert method == "GET"
    assert url == "https://8848-sbx.e2b.app/"


def test_next_asset_path_with_valid_cookie_is_proxied(client, monkeypatch):
    web_helper._ab_store("tok-ok", "8848-sbx.e2b.app")
    mock_response = httpx.Response(200, content=b"console.log(1)")
    mock_request = AsyncMock(return_value=mock_response)
    monkeypatch.setattr(web_helper._ab_http_client, "request", mock_request)

    resp = client.get("/_next/static/chunks/x.js", headers={"Cookie": f"{web_helper._AB_COOKIE}=tok-ok"})
    assert resp.status_code == 200
    assert resp.content == b"console.log(1)"


def test_root_path_without_a_cookie_serves_the_normal_info_page(client, monkeypatch):
    mock_request = AsyncMock()
    monkeypatch.setattr(web_helper._ab_http_client, "request", mock_request)

    resp = client.get("/")
    assert resp.status_code == 200
    assert "coolton web helper" in resp.text
    mock_request.assert_not_called()


def test_upload_route_is_never_hijacked_even_with_a_valid_cookie(client, monkeypatch):
    """A browser that still carries a live ab_session cookie from a past embed
    must not have unrelated web-helper routes silently proxied into a sandbox."""
    web_helper._ab_store("tok-ok", "8848-sbx.e2b.app")
    mock_request = AsyncMock()
    monkeypatch.setattr(web_helper._ab_http_client, "request", mock_request)

    resp = client.post("/upload", headers={"Cookie": f"{web_helper._AB_COOKIE}=tok-ok"})
    assert resp.status_code == 401  # falls through to /upload's own bearer-token check
    mock_request.assert_not_called()


def test_base64_html_embed_route_is_never_hijacked_even_with_a_valid_cookie(client, monkeypatch):
    web_helper._ab_store("tok-ok", "8848-sbx.e2b.app")
    mock_request = AsyncMock()
    monkeypatch.setattr(web_helper._ab_http_client, "request", mock_request)

    resp = client.get("/aGk", headers={"Cookie": f"{web_helper._AB_COOKIE}=tok-ok"})  # base64url for "hi"
    assert resp.status_code == 200
    assert resp.text == "hi"
    mock_request.assert_not_called()
