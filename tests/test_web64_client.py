from unittest.mock import Mock

import pytest
import requests

from agent.web64_client import upload_bytes


@pytest.fixture(autouse=True)
def fake_token(monkeypatch):
    monkeypatch.setattr("agent.web64_client._api_key", lambda: "tok-123")


def test_upload_bytes_returns_url(monkeypatch):
    resp = Mock()
    resp.json.return_value = {"url": "https://tanjim.org:2390/f/abc.html"}
    monkeypatch.setattr("agent.web64_client.requests.post", lambda *a, **k: resp)

    url = upload_bytes(b"<h1>hi</h1>", "embed.html", mime="text/html")
    assert url == "https://tanjim.org:2390/f/abc.html"


def test_upload_bytes_sends_auth_and_mime(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(url=url, headers=kwargs["headers"], params=kwargs["params"], data=kwargs["data"])
        resp = Mock()
        resp.json.return_value = {"url": "https://tanjim.org:2390/f/x"}
        return resp

    monkeypatch.setattr("agent.web64_client.requests.post", fake_post)
    upload_bytes(b"content", "a.txt", mime="text/plain")
    assert captured["url"] == "https://tanjim.org:2390/upload"
    assert captured["headers"]["Authorization"] == "Bearer tok-123"
    assert captured["headers"]["Content-Type"] == "text/plain"
    assert captured["params"] == {"filename": "a.txt"}
    assert captured["data"] == b"content"


def test_upload_bytes_no_mime_header(monkeypatch):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        resp = Mock()
        resp.json.return_value = {"url": "https://tanjim.org:2390/f/x"}
        return resp

    monkeypatch.setattr("agent.web64_client.requests.post", fake_post)
    upload_bytes(b"content", "a.txt")
    assert "Content-Type" not in captured["headers"]


def test_upload_bytes_http_error_propagates(monkeypatch):
    resp = Mock()
    resp.raise_for_status.side_effect = requests.HTTPError("401 Unauthorized")
    monkeypatch.setattr("agent.web64_client.requests.post", lambda *a, **k: resp)

    with pytest.raises(requests.HTTPError):
        upload_bytes(b"x", "a.txt")


def test_upload_bytes_missing_url_raises(monkeypatch):
    resp = Mock()
    resp.json.return_value = {"error": "no url"}
    monkeypatch.setattr("agent.web64_client.requests.post", lambda *a, **k: resp)

    with pytest.raises(RuntimeError, match="web64 upload failed"):
        upload_bytes(b"x", "a.txt")
