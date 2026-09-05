"""oauth_server.py: the shell-injection guard on _restart_coolton, and
_update_env's "0 lines changed" case actually being treated as a failure
instead of a silent no-op reported as success."""

from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

import oauth_server


@pytest.fixture
def client():
    return TestClient(oauth_server.app)


def _ok_exchange_result(user_id="UOWNER"):
    return {
        "ok": True,
        "access_token": "xoxb-new",
        "authed_user": {"id": user_id, "access_token": "xoxp-new"},
        "bot_user_id": "UBOT1",
    }


def test_restart_coolton_quotes_a_hostile_bot_user_id(monkeypatch):
    """bot_user_id comes straight from Slack's OAuth response and used to be
    interpolated directly into a `bash -c` string — a value like
    "; rm -rf /" would have run as a second shell command. It must now be
    shlex-quoted into a single inert argument, never spliced into the
    command as raw, unquoted shell syntax."""
    import shlex

    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        return Mock()

    monkeypatch.setattr(oauth_server.subprocess, "Popen", fake_popen)

    hostile = "UBOT1; touch /tmp/pwned"
    oauth_server._restart_coolton(hostile)

    assert captured["argv"][0:2] == ["bash", "-c"]
    cmd = captured["argv"][2]
    # The hostile value must be wrapped as a single shlex-quoted argument —
    # i.e. inside single quotes — so bash treats the whole thing (including
    # the ";") as one literal string argument to oauth_sync.py, never as
    # shell syntax that could run "touch /tmp/pwned" as its own command.
    assert shlex.quote(hostile) in cmd
    assert f"'{hostile}'" in cmd


def test_restart_coolton_normal_id_produces_a_sane_command(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        oauth_server.subprocess, "Popen",
        lambda argv, **kwargs: captured.setdefault("argv", argv) or Mock(),
    )

    oauth_server._restart_coolton("UBOT1")

    cmd = captured["argv"][2]
    assert "UBOT1" in cmd
    assert "systemctl restart coolton.service" in cmd


def test_oauth_redirect_fails_closed_when_update_env_changes_nothing(client, monkeypatch, tmp_path):
    """If .env has neither a SLACK_BOT_TOKEN= nor SLACK_USER_TOKEN= line,
    _update_env silently writes nothing back — this must be reported as a
    failure (and the service must NOT restart with stale tokens), not as the
    success message the handler used to return unconditionally."""
    env_path = tmp_path / ".env"
    env_path.write_text("SOME_OTHER_VAR=1\n")
    monkeypatch.setattr(oauth_server, "ENV_PATH", env_path)
    monkeypatch.setenv("COOLTON_USER_ID", "UOWNER")

    restarted = []
    monkeypatch.setattr(oauth_server, "_restart_coolton", lambda *a, **k: restarted.append(a))

    with patch("oauth_server._exchange", return_value=_ok_exchange_result("UOWNER")):
        resp = client.get("/slack/oauth_redirect", params={"code": "abc"})

    assert resp.status_code == 500
    assert "no SLACK_BOT_TOKEN" in resp.text
    assert restarted == []
    assert env_path.read_text() == "SOME_OTHER_VAR=1\n"  # untouched


def test_oauth_redirect_restarts_when_update_env_changes_lines(client, monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("SLACK_BOT_TOKEN=old\nSLACK_USER_TOKEN=old\n")
    monkeypatch.setattr(oauth_server, "ENV_PATH", env_path)
    monkeypatch.setenv("COOLTON_USER_ID", "UOWNER")

    restarted = []
    monkeypatch.setattr(oauth_server, "_restart_coolton", lambda *a, **k: restarted.append(a))

    with patch("oauth_server._exchange", return_value=_ok_exchange_result("UOWNER")):
        resp = client.get("/slack/oauth_redirect", params={"code": "abc"})

    assert resp.status_code == 200
    assert restarted == [("UBOT1",)]
    assert "SLACK_BOT_TOKEN=xoxb-new" in env_path.read_text()
