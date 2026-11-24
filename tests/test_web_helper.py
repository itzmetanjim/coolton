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


def test_nothing_but_uploaded_files_is_served(client):
    """The old /<base64> route rendered any HTML anyone put in a URL on a tanjim.org
    host; nothing used it, so it's gone."""

    encoded = base64.urlsafe_b64encode(b"<h1>hi</h1>").decode().rstrip("=")
    assert client.get(f"/{encoded}").status_code == 404


# ---------------------------------------------------------------------------
# /antibot/check
# ---------------------------------------------------------------------------


@pytest.fixture
def antibot(helper, monkeypatch):
    """The check route with two tokens configured and the private check stubbed out."""
    monkeypatch.setenv("ANTIBOT_API_TOKENS", "tok-a, tok-b")
    seen = []
    monkeypatch.setattr(helper, "_antibot_verdict",
                        lambda event: seen.append(event) or {"verdict": "watch", "categories": ["ios"]})
    return TestClient(helper.app), seen


MESSAGE = {"user": "U1", "ts": "1731510123.456789", "text": "hi", "blocks": []}


def test_antibot_needs_one_of_the_configured_tokens(antibot):
    client, seen = antibot
    assert client.post("/antibot/check", json={"message": MESSAGE}).status_code == 401
    assert client.post("/antibot/check", json={"message": MESSAGE},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401
    resp = client.post("/antibot/check", json={"message": MESSAGE}, headers={"Authorization": "Bearer tok-b"})
    assert resp.status_code == 200
    assert resp.json() == {"verdict": "watch", "categories": ["ios"]}
    assert seen[0]["type"] == "message"


def test_antibot_is_closed_when_no_token_is_configured(antibot, monkeypatch):
    client, _ = antibot
    monkeypatch.delenv("ANTIBOT_API_TOKENS")
    resp = client.post("/antibot/check", json={"message": MESSAGE}, headers={"Authorization": "Bearer "})
    assert resp.status_code == 401


def test_antibot_reads_a_permalinks_message_from_slack(antibot, helper, monkeypatch):
    client, seen = antibot
    asked = {}

    class Resp:
        def json(self):
            return {"ok": True, "messages": [{"type": "message", "ts": "1794054500.000001", "user": "U2"}]}

    monkeypatch.setattr(helper.requests, "get", lambda url, params, headers, timeout: asked.update({"url": url, **params}) or Resp())
    link = "https://x.slack.com/archives/C1/p1794054500000001?thread_ts=1794054476.315909&cid=C1"
    resp = client.post("/antibot/check", json={"permalink": link}, headers={"Authorization": "Bearer tok-a"})

    assert resp.json() == {"verdict": "watch", "categories": ["ios"]}
    assert asked["url"].endswith("/conversations.history")
    assert (asked["channel"], asked["oldest"], asked["latest"]) == ("C1", "1794054500.000001", "1794054500.000001")
    assert seen[0]["channel"] == "C1" and seen[0]["user"] == "U2"


def test_antibot_thread_permalink_falls_back_to_replies(antibot, helper, monkeypatch):
    client, seen = antibot
    asked = []

    def get(url, params, headers, timeout):
        asked.append((url, params))
        class Resp:
            def json(self):
                if url.endswith("conversations.history"):
                    return {"ok": True, "messages": []}
                return {"ok": True, "messages": [{"type": "message", "ts": "1794054500.000001", "user": "U3"}]}
        return Resp()

    monkeypatch.setattr(helper.requests, "get", get)
    link = "https://x.slack.com/archives/C1/p1794054500000001?thread_ts=1794054476.315909"
    resp = client.post("/antibot/check", json={"permalink": link},
                       headers={"Authorization": "Bearer tok-a"})

    assert resp.status_code == 200
    assert asked[0][0].endswith("conversations.history")
    assert asked[1][0].endswith("conversations.replies")
    assert asked[1][1]["ts"] == "1794054476.315909"
    assert asked[1][1]["oldest"] == "1794054500.000001"
    assert seen[0]["user"] == "U3"


def test_antibot_rejects_a_body_without_a_message(antibot):
    client, _ = antibot
    headers = {"Authorization": "Bearer tok-a"}
    assert client.post("/antibot/check", json={"text": "hi"}, headers=headers).status_code == 400
    assert client.post("/antibot/check", json={"permalink": "nope"}, headers=headers).status_code == 400
    assert client.post("/antibot/check", json={"calls": "not-an-array"}, headers=headers).status_code == 400
    assert client.post("/antibot/check", json={"calls": ["not-an-object"]}, headers=headers).status_code == 400


def test_antibot_accepts_a_batch_of_messages(antibot):
    client, seen = antibot
    headers = {"Authorization": "Bearer tok-a"}
    second = {"user": "U2", "ts": "1731510123.456790", "text": "bye", "blocks": []}

    resp = client.post("/antibot/check", json={"calls": [
        {"message": MESSAGE, "channel": "C1"},
        {"message": second},
    ]}, headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"responses": [
        {"verdict": "watch", "categories": ["ios"]},
        {"verdict": "watch", "categories": ["ios"]},
    ]}
    assert [event["user"] for event in seen] == ["U1", "U2"]
    assert seen[0]["channel"] == "C1"


def test_antibot_batch_isolates_errors(antibot, helper, monkeypatch):
    client, _ = antibot
    from fastapi import HTTPException

    def response(call):
        if call["message"]["text"] == "bad":
            raise HTTPException(status_code=404, detail="Couldn't read that message")
        return {"verdict": "none", "categories": []}

    monkeypatch.setattr(helper, "_antibot_response", response)
    resp = client.post("/antibot/check", json={"calls": [
        {"message": {**MESSAGE, "text": "good"}},
        {"message": {**MESSAGE, "text": "bad"}},
        {"message": {**MESSAGE, "text": "good"}},
    ]}, headers={"Authorization": "Bearer tok-a"})

    assert resp.status_code == 200
    assert resp.json() == {"responses": [
        {"verdict": "none", "categories": []},
        {"error": {"status": 404, "detail": "Couldn't read that message"}},
        {"verdict": "none", "categories": []},
    ]}


def test_antibot_accepts_an_empty_batch(antibot):
    client, _ = antibot
    resp = client.post("/antibot/check", json={"calls": []},
                       headers={"Authorization": "Bearer tok-a"})
    assert resp.status_code == 200
    assert resp.json() == {"responses": []}


def test_antibot_batch_runs_calls_in_parallel(antibot, helper, monkeypatch):
    client, seen = antibot
    import threading
    import time

    lock = threading.Lock()
    active = 0
    peak = 0

    def slow_response(call):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.1)
        with lock:
            active -= 1
        return {"verdict": "none", "categories": []}

    monkeypatch.setattr(helper, "_antibot_response", slow_response)
    resp = client.post("/antibot/check", json={"calls": [
        {"message": MESSAGE} for _ in range(helper._ANTIBOT_BATCH_CONCURRENCY + 5)
    ]}, headers={"Authorization": "Bearer tok-a"})

    assert resp.status_code == 200
    assert len(resp.json()["responses"]) == helper._ANTIBOT_BATCH_CONCURRENCY + 5
    assert peak > 1


def test_antibot_batch_accepts_permalinks_and_messages(antibot, helper, monkeypatch):
    client, seen = antibot

    class Resp:
        def json(self):
            return {"ok": True, "messages": [{"type": "message", "ts": "1794054500.000001", "user": "UPERM"}]}

    monkeypatch.setattr(helper.requests, "get", lambda url, params, headers, timeout: Resp())
    link = "https://x.slack.com/archives/C1/p1794054500000001"
    resp = client.post("/antibot/check", json={"calls": [
        {"permalink": link},
        {"message": MESSAGE, "channel": "C2"},
    ]}, headers={"Authorization": "Bearer tok-a"})

    assert resp.status_code == 200
    assert resp.json() == {"responses": [
        {"verdict": "watch", "categories": ["ios"]},
        {"verdict": "watch", "categories": ["ios"]},
    ]}
    assert [event["user"] for event in seen] == ["UPERM", "U1"]



def test_upload_over_the_limit_is_refused_and_leaves_nothing(helper, client, tmp_path, monkeypatch):
    monkeypatch.setattr(helper, "MAX_UPLOAD_BYTES", 4)
    resp = client.post("/upload?filename=x.bin", content=b"12345", headers={"Authorization": "Bearer s3cr3t-token"})
    assert resp.status_code == 413
    files = tmp_path / "files"
    assert not files.exists() or not list(files.iterdir())

    # A body bigger than its Content-Length says is cut off as it streams in.
    def chunks():
        yield b"123"
        yield b"45"
    streamed = client.post("/upload?filename=x.bin", content=chunks(), headers={"Authorization": "Bearer s3cr3t-token"})
    assert streamed.status_code == 413
    assert not files.exists() or not list(files.iterdir())
    ok = client.post("/upload?filename=x.bin", content=b"1234", headers={"Authorization": "Bearer s3cr3t-token"})
    assert ok.status_code == 200 and len(list((tmp_path / "files").iterdir())) == 1


def test_private_routes_live_only_on_their_own_host(helper, client, monkeypatch):
    """Hosted /f/ pages must never share an origin with the private module's pages."""
    @helper.app.get("/private-page")
    async def private_page():
        return {"ok": True}

    monkeypatch.setattr(helper, "_PRIVATE_PATHS", {"/private-page"})
    assert client.get("/private-page").status_code == 200           # off until a host is set
    monkeypatch.setattr(helper, "PRIVATE_HOST", "verify.example.test")
    private = {"host": helper.PRIVATE_HOST}
    proxied = {"host": "matrix.tanjim.org", "x-forwarded-host": helper.PRIVATE_HOST}

    assert client.get("/private-page").status_code == 404           # the file host
    assert client.get("/private-page", headers=private).status_code == 200
    assert client.get("/private-page", headers=proxied).status_code == 200
    assert client.get("/f/anything", headers=private).status_code == 404
    assert client.get("/", headers=private).status_code == 404
    assert client.get("/").status_code == 200
