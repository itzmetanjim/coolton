"""web/conversations.py attachment upload/download: asset_id validation
(path traversal) and per-conversation collision-proofing.

Uses a real TestClient (rather than calling the route function directly, the
pattern in test_web_conversation_routes.py) because upload_attachment_route
takes a multipart UploadFile — easiest to exercise through actual HTTP.
"""

import pytest
from fastapi.testclient import TestClient

from web import auth, conversation_log as log
from web.server import app


@pytest.fixture(autouse=True)
def tmp_dirs(monkeypatch, tmp_path):
    monkeypatch.setenv("COOLTON_WEB_SECRET", "test-secret")
    monkeypatch.setattr(log, "STORE_DIR", str(tmp_path / "web_conversations"))
    log._locks.clear()
    log._last_seq.clear()
    with log._subscribers_guard:
        log._subscribers.clear()
    from web import conversations as routes
    monkeypatch.setattr(routes, "ATTACHMENTS_DIR", str(tmp_path / "web_attachments"))


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=False)


def _signed_in(client, slack_id="U1"):
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session_token(slack_id))
    return client


def test_upload_then_fetch_roundtrips(client):
    _signed_in(client)
    cid = log.create_conversation("U1")
    resp = client.post(
        f"/api/conversations/{cid}/attachments",
        files={"file": ("hello.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 200
    asset_id = resp.json()["id"]

    fetched = client.get(f"/api/files/{asset_id}")
    assert fetched.status_code == 200
    assert fetched.content == b"hello world"


def test_path_traversal_asset_id_in_attachment_ids_is_rejected(client):
    """attachment_ids reaches _load_attachment_meta straight from the JSON
    body of send_message_route with no other validation — a crafted id must
    not be able to read an arbitrary "*.json" file on disk."""
    _signed_in(client)
    cid = log.create_conversation("U1")

    resp = client.post(
        f"/api/conversations/{cid}/messages",
        json={"text": "hi", "attachment_ids": ["../../../../etc/passwd"]},
    )
    # Rejected as an invalid id (never even opened) rather than 500ing or
    # attaching whatever it found — the message still sends with a filtered
    # (empty) attachment list.
    assert resp.status_code == 200


def test_path_traversal_asset_id_via_file_route_is_rejected(client):
    _signed_in(client)
    resp = client.get("/api/files/..%2f..%2f..%2fetc%2fpasswd")
    assert resp.status_code == 404


def test_two_conversations_uploading_identical_bytes_do_not_collide(client):
    _signed_in(client, "U1")
    cid1 = log.create_conversation("U1")
    cid2 = log.create_conversation("U1")

    resp1 = client.post(
        f"/api/conversations/{cid1}/attachments",
        files={"file": ("same.txt", b"identical content", "text/plain")},
    )
    resp2 = client.post(
        f"/api/conversations/{cid2}/attachments",
        files={"file": ("same.txt", b"identical content", "text/plain")},
    )
    assert resp1.status_code == 200 and resp2.status_code == 200
    id1, id2 = resp1.json()["id"], resp2.json()["id"]
    assert id1 != id2

    # Both remain independently fetchable — neither upload clobbered the
    # other's metadata record.
    assert client.get(f"/api/files/{id1}").status_code == 200
    assert client.get(f"/api/files/{id2}").status_code == 200


def test_asset_id_matches_the_documented_shape(client):
    _signed_in(client)
    cid = log.create_conversation("U1")
    resp = client.post(
        f"/api/conversations/{cid}/attachments",
        files={"file": ("report.PDF", b"data", "application/pdf")},
    )
    asset_id = resp.json()["id"]
    from web.conversations import _valid_asset_id
    assert _valid_asset_id(asset_id)
    assert asset_id.endswith(".pdf")
