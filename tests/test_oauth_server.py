"""oauth_server.py: the shell-injection guard on _restart_coolton, and
_update_env's "0 lines changed" case actually being treated as a failure
instead of a silent no-op reported as success."""

from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

import oauth_server


@pytest.fixture(autouse=True)
def _isolated_error_log(tmp_path, monkeypatch):
    """Failed redirects append to oauth_errors.log: keep it out of the checkout."""
    monkeypatch.setattr(oauth_server, "ERROR_LOG_PATH", tmp_path / "oauth_errors.log")


@pytest.fixture
def client():
    """A browser that started the install here, so the callback's state matches."""
    browser = TestClient(oauth_server.app, base_url="https://testserver", follow_redirects=False)
    start = browser.get("/")
    browser.state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    return browser


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
        resp = client.get("/slack/oauth_redirect", params={"code": "abc", "state": client.state})

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
        resp = client.get("/slack/oauth_redirect", params={"code": "abc", "state": client.state})

    assert resp.status_code == 200
    assert restarted == [("UBOT1",)]
    assert "SLACK_BOT_TOKEN=xoxb-new" in env_path.read_text()


def test_a_failed_install_never_logs_the_tokens_slack_sent(client, monkeypatch, tmp_path):
    """The wrong person completing the install still gets real tokens back from Slack;
    those must not land in oauth_errors.log."""
    import json as _json

    monkeypatch.setenv("COOLTON_USER_ID", "UOWNER")
    with patch.object(oauth_server, "_exchange", return_value=_ok_exchange_result(user_id="USOMEONE_ELSE")):
        resp = client.get("/slack/oauth_redirect", params={"code": "abc", "state": client.state})
    assert resp.status_code == 403
    logged = oauth_server.ERROR_LOG_PATH.read_text()
    assert "xoxb-new" not in logged and "xoxp-new" not in logged
    assert _json.loads(logged.splitlines()[-1])["authed_user_id"] == "USOMEONE_ELSE"


def test_rewriting_env_keeps_it_private(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("SLACK_BOT_TOKEN=old\nSLACK_USER_TOKEN=old\n")
    env.chmod(0o600)
    monkeypatch.setattr(oauth_server, "ENV_PATH", env)

    assert oauth_server._update_env("xoxb-new", "xoxp-new") == 2
    assert oct(env.stat().st_mode & 0o777) == "0o600"


def test_the_callback_only_accepts_an_install_started_here(client, monkeypatch):
    """Without the state check, anyone could send cooltonUser a crafted install link (with
    fewer scopes, say) that rotates coolton's tokens and restarts it."""
    exchange = Mock(return_value=_ok_exchange_result("UOWNER"))
    monkeypatch.setattr(oauth_server, "_exchange", exchange)
    stranger = TestClient(oauth_server.app, base_url="https://testserver", follow_redirects=False)
    for browser, state in ((stranger, client.state), (client, "forged"), (client, "")):
        resp = browser.get("/slack/oauth_redirect", params={"code": "abc", "state": state})
        assert resp.status_code == 400
    assert not exchange.called
