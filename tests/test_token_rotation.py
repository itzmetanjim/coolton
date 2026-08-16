import json
import time

import pytest

from agent import redact
from agent import token_rotation


@pytest.fixture
def tokens_file(monkeypatch, tmp_path):
    path = tmp_path / "xoxe_tokens.json"
    monkeypatch.setattr(token_rotation, "TOKENS_FILE", path)
    monkeypatch.setattr(token_rotation, "_cache", None)
    monkeypatch.setattr(token_rotation, "_rotating", False)
    monkeypatch.setattr(redact, "_XOXE_TOKENS_PATH", path)
    monkeypatch.setattr(redact, "_ENV_PATH", tmp_path / "env")
    monkeypatch.setattr(redact, "_secret_values_cache", None)
    return path


def _write_tokens(path, access="xoxe.xoxp-1-old", refresh="xoxe-1-refresh-old", expires_at=None):
    data = {"access_token": access, "refresh_token": refresh}
    if expires_at is not None:
        data["expires_at"] = expires_at
    path.write_text(json.dumps(data))


def _mock_rotate(monkeypatch, payload):
    class FakeResp:
        def json(self):
            return payload

    monkeypatch.setattr(token_rotation.requests, "post", lambda *a, **k: FakeResp())


# ---------------------------------------------------------------------------
# getters
# ---------------------------------------------------------------------------


def test_getters_read_json(tokens_file):
    _write_tokens(tokens_file, expires_at=123)
    assert token_rotation.get_access_token() == "xoxe.xoxp-1-old"
    assert token_rotation.get_refresh_token() == "xoxe-1-refresh-old"
    assert token_rotation.get_expires_at() == 123


def test_getters_empty_without_file(tokens_file):
    assert token_rotation.get_access_token() is None
    assert token_rotation.get_refresh_token() is None
    assert token_rotation.get_expires_at() is None


# ---------------------------------------------------------------------------
# rotate_token
# ---------------------------------------------------------------------------


def test_rotate_token_persists_new_pair(tokens_file, monkeypatch):
    _write_tokens(tokens_file)
    _mock_rotate(
        monkeypatch,
        {
            "ok": True,
            "token": "xoxe.xoxp-1-new",
            "refresh_token": "xoxe-1-refresh-new",
            "team_id": "T1",
            "user_id": "U1",
            "iat": 100,
            "exp": 100 + 43200,
        },
    )
    assert token_rotation.rotate_token() is True
    assert token_rotation.get_access_token() == "xoxe.xoxp-1-new"
    assert token_rotation.get_refresh_token() == "xoxe-1-refresh-new"
    assert token_rotation.get_expires_at() == 100 + 43200
    assert json.loads(tokens_file.read_text()) == {
        "access_token": "xoxe.xoxp-1-new",
        "refresh_token": "xoxe-1-refresh-new",
        "expires_at": 100 + 43200,
    }


def test_rotate_token_no_refresh_token(tokens_file):
    tokens_file.write_text("{}")
    assert token_rotation.rotate_token() is False


def test_rotate_token_api_error(tokens_file, monkeypatch):
    _write_tokens(tokens_file)
    _mock_rotate(monkeypatch, {"ok": False, "error": "invalid_refresh_token"})
    assert token_rotation.rotate_token() is False


# ---------------------------------------------------------------------------
# check_and_rotate
# ---------------------------------------------------------------------------


def test_check_and_rotate_skips_when_far_from_expiry(tokens_file, monkeypatch):
    _write_tokens(tokens_file, expires_at=int(time.time()) + 11 * 3600)
    called = []

    def _fake(*a, **k):
        called.append(1)
        return type("R", (), {"json": lambda self: {"ok": False}})()

    monkeypatch.setattr(token_rotation.requests, "post", _fake)
    token_rotation.check_and_rotate()
    assert called == []


def test_check_and_rotate_rotates_when_expiring_soon(tokens_file, monkeypatch):
    _write_tokens(tokens_file, expires_at=int(time.time()) + 3600)
    _mock_rotate(
        monkeypatch,
        {
            "ok": True,
            "token": "xoxe.xoxp-1-new",
            "refresh_token": "xoxe-1-refresh-new",
            "exp": int(time.time()) + 43200,
        },
    )
    token_rotation.check_and_rotate()
    assert token_rotation.get_access_token() == "xoxe.xoxp-1-new"


def test_check_and_rotate_rotates_when_expiry_unknown(tokens_file, monkeypatch):
    _write_tokens(tokens_file)
    _mock_rotate(
        monkeypatch,
        {
            "ok": True,
            "token": "xoxe.xoxp-1-new",
            "refresh_token": "xoxe-1-refresh-new",
            "exp": int(time.time()) + 43200,
        },
    )
    token_rotation.check_and_rotate()
    assert token_rotation.get_access_token() == "xoxe.xoxp-1-new"


def test_check_and_rotate_noop_without_refresh(tokens_file):
    _write_tokens(tokens_file, access="xoxe.xoxp-1-old", refresh="")
    token_rotation.check_and_rotate()
    assert token_rotation.get_access_token() == "xoxe.xoxp-1-old"


# ---------------------------------------------------------------------------
# redactor integration
# ---------------------------------------------------------------------------


def test_redactor_masks_tokens_stored_in_json_file(tokens_file):
    _write_tokens(tokens_file, access="xoxe.xoxp-1-abc", refresh="xoxe-1-def")
    redact.invalidate_secret_cache()
    assert redact.redact("leaked xoxe.xoxp-1-abc and xoxe-1-def") == "leaked *** and ***"


def test_redactor_masks_rotated_token_after_invalidation(tokens_file):
    _write_tokens(tokens_file, access="xoxe.xoxp-1-old", refresh="xoxe-1-old-r")
    redact.invalidate_secret_cache()
    assert redact.redact("old xoxe.xoxp-1-old") == "old ***"
    assert redact.redact("new xoxe.xoxp-1-new") == "new xoxe.xoxp-1-new"

    _write_tokens(tokens_file, access="xoxe.xoxp-1-new", refresh="xoxe-1-new-r")
    redact.invalidate_secret_cache()
    assert redact.redact("new xoxe.xoxp-1-new") == "new ***"
