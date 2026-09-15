from unittest.mock import Mock

import pytest

from agent.tools import slack_emoji


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv("EMOJI_PROXY_TOKEN", "proxy-token")


def _fake_response(ok=True, status_code=200, text=""):
    resp = Mock()
    resp.ok = ok
    resp.status_code = status_code
    resp.text = text
    return resp


def test_upload_emoji_not_configured_without_token(monkeypatch):
    monkeypatch.delenv("EMOJI_PROXY_TOKEN", raising=False)
    result = slack_emoji.upload_emoji("C1", "1.1", "party", path="/home/user/party.png")
    assert result.startswith("Error:")
    assert "EMOJI_PROXY_TOKEN" in result


def test_upload_emoji_rejects_invalid_name():
    result = slack_emoji.upload_emoji("C1", "1.1", "Not Valid!", path="/home/user/x.png")
    assert result.startswith("Error:")


def test_upload_emoji_requires_exactly_one_of_path_or_alias():
    assert slack_emoji.upload_emoji("C1", "1.1", "party").startswith("Error:")
    assert slack_emoji.upload_emoji(
        "C1", "1.1", "party", path="/x.png", alias_for="tada"
    ).startswith("Error:")


def test_upload_emoji_alias_posts_to_the_alias_endpoint(monkeypatch):
    captured = {}

    def fake_post(url, headers, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _fake_response()

    monkeypatch.setattr(slack_emoji.requests, "post", fake_post)
    result = slack_emoji.upload_emoji("C1", "1.1", "party2", alias_for="party")

    assert result == "Added :party2:."
    assert captured["url"].endswith("/alias")
    assert captured["json"] == {"name": "party2", "alias_for": "party"}


def test_upload_emoji_new_image_reads_from_sandbox_and_posts_file(monkeypatch):
    fake_sandbox = Mock()
    fake_sandbox.files.read.return_value = b"pngbytes"
    monkeypatch.setattr(
        "agent.sandbox_helpers.get_or_create_sandbox",
        lambda channel_id, thread_ts: (fake_sandbox, "sbx1"),
    )
    captured = {}

    def fake_post(url, headers, files, data, timeout):
        captured["url"] = url
        captured["files"] = files
        captured["data"] = data
        return _fake_response()

    monkeypatch.setattr(slack_emoji.requests, "post", fake_post)
    result = slack_emoji.upload_emoji("C1", "1.1", "party", path="/home/user/party.png")

    assert result == "Added :party:."
    assert captured["url"].endswith("/upload")
    assert captured["data"] == {"name": "party"}
    assert captured["files"]["file"][0] == "party.png"
    fake_sandbox.files.read.assert_called_once_with("/home/user/party.png", format="bytes")


def test_upload_emoji_reports_a_non_ok_response(monkeypatch):
    monkeypatch.setattr(
        slack_emoji.requests, "post",
        lambda *a, **k: _fake_response(ok=False, status_code=409, text="already exists"),
    )
    result = slack_emoji.upload_emoji("C1", "1.1", "party", alias_for="tada")
    assert "409" in result
    assert "already exists" in result


def test_upload_emoji_swallows_and_reports_request_exceptions(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(slack_emoji.requests, "post", boom)
    result = slack_emoji.upload_emoji("C1", "1.1", "party", alias_for="tada")
    assert "network down" in result
