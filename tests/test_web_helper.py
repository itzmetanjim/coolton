import base64
import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def helper(tmp_path, monkeypatch):
    """Fresh coolton_web_helper module with an isolated token file + files dir."""
    token_file = tmp_path / "token"
    token_file.write_text("s3cr3t-token\n")
    monkeypatch.setenv("WEB_HELPER_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("WEB_HELPER_FILES_DIR", str(tmp_path / "files"))
    monkeypatch.setenv("WEB_HELPER_BASE_URL", "https://example.test")

    import coolton_web_helper as w
    importlib.reload(w)
    return w


@pytest.fixture
def client(helper):
    return TestClient(helper.app)


def test_upload_requires_correct_bearer_token(client):
    resp = client.post("/upload?filename=x.html", content=b"hi", headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_upload_rejects_missing_authorization_header(client):
    resp = client.post("/upload?filename=x.html", content=b"hi")
    assert resp.status_code == 401


def test_upload_succeeds_with_correct_bearer_token(client):
    resp = client.post("/upload?filename=x.html", content=b"hi", headers={"Authorization": "Bearer s3cr3t-token"})
    assert resp.status_code == 200
    assert resp.json()["url"].startswith("https://example.test/f/")


def test_upload_rejects_empty_bearer_when_token_file_missing(tmp_path, monkeypatch):
    """Regression: when the token file is missing/unreadable, _api_key() used to return
    "" and the auth check became `auth == "Bearer "` — trivially satisfiable by anyone
    sending an empty bearer token. Must fail closed instead: no configured key means
    no request is ever authorized."""
    monkeypatch.setenv("WEB_HELPER_TOKEN_FILE", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("WEB_HELPER_FILES_DIR", str(tmp_path / "files"))

    import coolton_web_helper as w
    importlib.reload(w)
    client = TestClient(w.app)

    resp = client.post("/upload?filename=x.html", content=b"hi", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401

    resp2 = client.post("/upload?filename=x.html", content=b"hi", headers={"Authorization": ""})
    assert resp2.status_code == 401


def test_upload_rejects_empty_body(client):
    resp = client.post("/upload?filename=x.html", content=b"", headers={"Authorization": "Bearer s3cr3t-token"})
    assert resp.status_code == 400


def test_uploaded_file_is_served_back(client):
    upload_resp = client.post(
        "/upload?filename=hello.txt", content=b"hello world",
        headers={"Authorization": "Bearer s3cr3t-token"},
    )
    path = upload_resp.json()["path"]
    get_resp = client.get(path)
    assert get_resp.status_code == 200
    assert get_resp.content == b"hello world"


def test_serve_file_rejects_path_traversal(client):
    resp = client.get("/f/..%2F..%2Fetc%2Fpasswd")
    assert resp.status_code == 404


def test_serve_file_404_for_unknown_name(client):
    resp = client.get("/f/does-not-exist.txt")
    assert resp.status_code == 404


def test_decode_base64_html_renders_content(client):
    encoded = base64.urlsafe_b64encode(b"<h1>hi</h1>").decode().rstrip("=")
    resp = client.get(f"/{encoded}")
    assert resp.status_code == 200
    assert "<h1>hi</h1>" in resp.text


def test_decode_base64_html_rejects_invalid_input(client):
    resp = client.get("/not valid base64!!!")
    assert resp.status_code == 400
