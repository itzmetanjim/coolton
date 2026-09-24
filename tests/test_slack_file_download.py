from unittest.mock import Mock


from agent.tools.slack_file_download import download_file_by_id, is_slack_host


# ---------------------------------------------------------------------------
# is_slack_host
# ---------------------------------------------------------------------------


def test_slack_hosts_recognized():
    assert is_slack_host("https://slack.com/files/team/F123/download") is True
    assert is_slack_host("https://files.slack.com/files-pri/T123-F123/download") is True
    assert is_slack_host("https://foo.slack-files.com/x/y") is True


def test_non_slack_hosts_rejected():
    assert is_slack_host("https://evil.com/x") is False
    assert is_slack_host("https://slack.com.evil.com/x") is False
    assert is_slack_host("https://notslack-files.com/x") is False
    assert is_slack_host("not a url") is False


# ---------------------------------------------------------------------------
# download_file_by_id
# ---------------------------------------------------------------------------


def test_requires_token(monkeypatch):
    monkeypatch.delenv("SLACK_USER_TOKEN", raising=False)
    result = download_file_by_id("F12345678")
    assert "SLACK_USER_TOKEN not configured" in result


def test_rejects_non_slack_file_ids(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-token")
    assert "Not a Slack file id" in download_file_by_id("not-an-id")


def test_permalink_extracts_file_id(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-token")
    seen = {}

    def fake_get(url, **kwargs):
        seen["params"] = kwargs.get("params")
        return Mock(json=lambda: {"ok": True, "file": {"name": "x.txt"}})

    monkeypatch.setattr("agent.tools.slack_file_download.requests.get", fake_get)
    download_file_by_id("https://slack.com/files/x/F12345678/name", user_token="xoxp-token")
    assert seen["params"]["file"] == "F12345678"


def test_missing_scope_hint(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-token")

    def fake_get(url, **kwargs):
        return Mock(json=lambda: {"ok": False, "error": "missing_scope"})

    monkeypatch.setattr("agent.tools.slack_file_download.requests.get", fake_get)
    result = download_file_by_id("F12345678")
    assert "missing the `files:read` scope" in result


def test_refuses_non_slack_download_url(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-token")

    def fake_get(url, **kwargs):
        if "files.info" in url:
            return Mock(json=lambda: {
                "ok": True,
                "file": {"url_private_download": "https://evil.com/file", "name": "x.txt", "is_public": True},
            })
        return Mock(content=b"data", status_code=200)

    monkeypatch.setattr("agent.tools.slack_file_download.requests.get", fake_get)
    result = download_file_by_id("F12345678")
    assert "Refusing to download from a non-Slack host" in result


def test_success_to_sandbox_sanitizes_filename(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-token")
    sandbox = Mock()

    def fake_get(url, **kwargs):
        if "files.info" in url:
            return Mock(json=lambda: {
                "ok": True,
                "file": {
                    "is_public": True,
                    "url_private_download": "https://files.slack.com/files-pri/T123-F123/x",
                    "name": "../evil.sh",
                    "mimetype": "text/plain",
                },
            })
        return Mock(content=b"#!/bin/sh\n", status_code=200)

    monkeypatch.setattr("agent.tools.slack_file_download.requests.get", fake_get)

    result = download_file_by_id("F12345678", user_token="xoxp-token", sandbox=sandbox)
    assert ".._evil.sh" in result
    # path separators were scrubbed so nothing escapes ~/downloads/
    assert "/home/user/downloads/.._evil.sh" in str(sandbox.files.write.call_args)


def test_success_returns_base64_summary(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-token")

    def fake_get(url, **kwargs):
        if "files.info" in url:
            return Mock(json=lambda: {
                "ok": True,
                "file": {
                    "is_public": True,
                    "url_private_download": "https://files.slack.com/files-pri/T123-F123/x",
                    "name": "notes.txt",
                    "mimetype": "text/plain",
                },
            })
        return Mock(content=b"hello", status_code=200)

    monkeypatch.setattr("agent.tools.slack_file_download.requests.get", fake_get)
    result = download_file_by_id("F12345678", user_token="xoxp-token")
    assert result.startswith("Downloaded notes.txt (5 bytes)")
    assert "Base64: aGVsbG8=" in result


def test_timeout_handled(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-token")
    import requests

    def fake_get(url, **kwargs):
        raise requests.Timeout("timed out")

    monkeypatch.setattr("agent.tools.slack_file_download.requests.get", fake_get)
    assert "Download timed out" in download_file_by_id("F12345678", user_token="xoxp-token")


def test_slack_api_error(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-token")

    def fake_get(url, **kwargs):
        return Mock(json=lambda: {"ok": False, "error": "file_not_found"})

    monkeypatch.setattr("agent.tools.slack_file_download.requests.get", fake_get)
    assert "file_not_found" in download_file_by_id("F12345678", user_token="xoxp-token")


def test_no_download_url(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-token")

    def fake_get(url, **kwargs):
        return Mock(json=lambda: {"ok": True, "file": {"name": "x.txt", "is_public": True}})

    monkeypatch.setattr("agent.tools.slack_file_download.requests.get", fake_get)
    assert "No download URL available" in download_file_by_id("F12345678", user_token="xoxp-token")


# ---------------------------------------------------------------------------
# download_file_by_id — only files the person asking could see themselves
# ---------------------------------------------------------------------------


def _file_info_then_bytes(monkeypatch, file_info):
    downloads = []

    def fake_get(url, **kwargs):
        if "files.info" in url:
            return Mock(json=lambda: {"ok": True, "file": {
                "url_private_download": "https://files.slack.com/files-pri/T1-F1/x",
                "name": "secret.txt", **file_info,
            }})
        downloads.append(url)
        return Mock(content=b"data", status_code=200)

    monkeypatch.setattr("agent.tools.slack_file_download.requests.get", fake_get)
    return downloads


def test_refuses_a_file_only_shared_in_a_private_channel(monkeypatch):
    downloads = _file_info_then_bytes(monkeypatch, {
        "user": "U_UPLOADER", "groups": ["G_PRIVATE"], "shares": {"private": {"G_PRIVATE": []}},
    })
    result = download_file_by_id(
        "F12345678", user_token="xoxp-token", current_channel_id="C_HERE", requester_id="U_ASKER",
    )
    assert result.startswith("Error: can't download")
    assert downloads == []


def test_allows_a_private_file_shared_in_the_current_conversation(monkeypatch):
    _file_info_then_bytes(monkeypatch, {"shares": {"private": {"G_HERE": []}}})
    result = download_file_by_id(
        "F12345678", user_token="xoxp-token", current_channel_id="G_HERE", requester_id="U_ASKER",
    )
    assert result.startswith("Downloaded secret.txt")


def test_allows_the_requesters_own_upload(monkeypatch):
    _file_info_then_bytes(monkeypatch, {"user": "U_ASKER", "ims": ["D_ELSEWHERE"]})
    result = download_file_by_id(
        "F12345678", user_token="xoxp-token", current_channel_id="C_HERE", requester_id="U_ASKER",
    )
    assert result.startswith("Downloaded secret.txt")
