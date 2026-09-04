from unittest.mock import Mock, patch

import pytest

from web import auth


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("COOLTON_WEB_SECRET", "test-secret")
    monkeypatch.setenv("HCA_CLIENT_ID", "client-id")
    monkeypatch.setenv("HCA_CLIENT_SECRET", "client-secret")


def test_sign_and_verify_round_trip():
    token = auth._sign({"slack_id": "U1", "issued_at": 100.0})
    payload = auth._verify(token)
    assert payload == {"slack_id": "U1", "issued_at": 100.0}


def test_verify_rejects_a_tampered_payload():
    token = auth._sign({"slack_id": "U1", "issued_at": 100.0})
    sig = token.split(".", 1)[1]
    tampered = auth._sign({"slack_id": "ATTACKER", "issued_at": 100.0}).split(".", 1)[0]
    assert auth._verify(f"{tampered}.{sig}") is None


def test_verify_rejects_garbage():
    assert auth._verify("not-a-real-token") is None
    assert auth._verify("") is None


def test_verify_fails_closed_with_no_secret_configured(monkeypatch):
    monkeypatch.delenv("COOLTON_WEB_SECRET", raising=False)
    token = auth._sign({"slack_id": "U1", "issued_at": 100.0})
    assert auth._verify(token) is None


def test_get_session_reads_a_valid_cookie():
    token = auth.create_session_token("U1")
    request = Mock()
    request.cookies = {auth.SESSION_COOKIE: token}
    session = auth.get_session(request)
    assert session["slack_id"] == "U1"


def test_get_session_none_without_a_cookie():
    request = Mock()
    request.cookies = {}
    assert auth.get_session(request) is None


def test_get_session_rejects_an_expired_session():
    token = auth._sign({"slack_id": "U1", "issued_at": 0.0})
    request = Mock()
    request.cookies = {auth.SESSION_COOKIE: token}
    assert auth.get_session(request) is None


def test_require_slack_id_returns_none_when_signed_out():
    request = Mock()
    request.cookies = {}
    assert auth.require_slack_id(request) is None


def test_require_slack_id_returns_the_id_when_signed_in():
    request = Mock()
    request.cookies = {auth.SESSION_COOKIE: auth.create_session_token("U1")}
    assert auth.require_slack_id(request) == "U1"


def test_authorize_url_requests_only_the_slack_id_scope():
    url = auth._authorize_url("some-state")
    assert "scope=slack_id" in url
    assert "client_id=client-id" in url
    assert "some-state" in url


def test_login_sets_a_state_cookie_and_redirects_to_hackclub():
    response = auth.login()
    assert response.status_code == 302
    assert response.headers["location"].startswith(auth.AUTHORIZE_URL)
    assert "oauth_state=" in response.headers.get("set-cookie", "")


def test_callback_rejects_a_mismatched_state():
    request = Mock()
    request.cookies = {auth.STATE_COOKIE: "expected"}
    response = auth.callback(request, code="abc", state="different")
    assert response.status_code == 302
    assert "auth_error" in response.headers["location"]


def test_callback_rejects_a_missing_code():
    request = Mock()
    request.cookies = {auth.STATE_COOKIE: "expected"}
    response = auth.callback(request, code="", state="expected")
    assert "auth_error" in response.headers["location"]


def test_callback_sets_a_session_cookie_on_success():
    request = Mock()
    request.cookies = {auth.STATE_COOKIE: "expected"}
    with patch.object(auth, "_exchange_code", return_value={"access_token": "tok"}), \
         patch.object(auth, "_fetch_slack_id", return_value="U42"):
        response = auth.callback(request, code="abc", state="expected")
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    set_cookies = response.headers.getlist("set-cookie")
    assert any(auth.SESSION_COOKIE in c for c in set_cookies)


def test_callback_redirects_with_error_when_no_slack_id_comes_back():
    request = Mock()
    request.cookies = {auth.STATE_COOKIE: "expected"}
    with patch.object(auth, "_exchange_code", return_value={"access_token": "tok"}), \
         patch.object(auth, "_fetch_slack_id", return_value=None):
        response = auth.callback(request, code="abc", state="expected")
    assert "auth_error" in response.headers["location"]
    set_cookies = response.headers.getlist("set-cookie")
    assert not any(auth.SESSION_COOKIE in c for c in set_cookies)


def test_secure_cookies_false_for_localhost_redirect_uri(monkeypatch):
    monkeypatch.setenv("HCA_REDIRECT_URI", "http://localhost:8000/oauth/callback")
    assert auth._secure_cookies() is False


def test_secure_cookies_true_for_https_redirect_uri(monkeypatch):
    monkeypatch.setenv("HCA_REDIRECT_URI", "https://coolton.tanjim.org/oauth/callback")
    assert auth._secure_cookies() is True
