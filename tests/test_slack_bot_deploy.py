import json
from unittest.mock import Mock
from urllib.parse import parse_qs, quote, urlparse

import pytest

from agent.tools import slack_bot_deploy as sbd


@pytest.fixture(autouse=True)
def web_secret(monkeypatch):
    monkeypatch.setenv("COOLTON_WEB_SECRET", "test-secret")


def _seed_bot(monkeypatch, uuid="app123", bot_token="xoxb-1", app_token="xapp-1"):
    monkeypatch.setattr(
        sbd,
        "_load",
        lambda: {uuid: {"app_id": uuid, "bot_token": bot_token, "app_token": app_token}},
    )


# ---------------------------------------------------------------------------
# register_bot_tokens — only accepts real bot/app tokens, never a user token
# ---------------------------------------------------------------------------


def test_register_bot_tokens_rejects_non_bot_token(monkeypatch, tmp_path):
    monkeypatch.setattr(sbd, "STORE", tmp_path / "bots.json")
    monkeypatch.setattr(sbd, "_load", lambda: {"app123": {"app_id": "app123"}})
    result = sbd.register_bot_tokens("app123", "xoxp-not-a-bot-token", "xapp-1")
    assert "only xoxb- bot tokens are accepted" in result


def test_register_bot_tokens_rejects_unknown_uuid(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {})
    result = sbd.register_bot_tokens("unknown", "xoxb-1", "xapp-1")
    assert "unknown bot UUID" in result


def test_register_bot_tokens_app_token_is_optional(monkeypatch):
    """HTTP-mode Workers (what wrangler_bot_deploy targets) never get an xapp- token
    via OAuth install — it's a manual, Socket-Mode-only credential."""
    saved = {}
    monkeypatch.setattr(sbd, "_load", lambda: {"app123": {"app_id": "app123"}})
    monkeypatch.setattr(sbd, "_save", lambda data: saved.update(data))
    result = sbd.register_bot_tokens("app123", "xoxb-1")
    assert "registered" in result
    assert saved["app123"]["bot_token"] == "xoxb-1"
    assert "app_token" not in saved["app123"]


def test_register_bot_tokens_rejects_malformed_app_token_when_given(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {"app123": {"app_id": "app123"}})
    result = sbd.register_bot_tokens("app123", "xoxb-1", "not-an-xapp-token")
    assert "app_token must start with xapp-" in result


# ---------------------------------------------------------------------------
# sign_install_state / verify_install_state — binds an OAuth install attempt
# to the app it was created for, so a forged/foreign `state` can't register a
# captured token onto an app it doesn't belong to.
# ---------------------------------------------------------------------------


def test_state_round_trips_to_the_signed_app_id():
    state = sbd.sign_install_state("A123")
    assert sbd.verify_install_state(state) == "A123"


def test_state_rejects_garbage():
    assert sbd.verify_install_state("not-a-real-state") is None
    assert sbd.verify_install_state("") is None


def test_state_rejects_a_tampered_signature():
    state = sbd.sign_install_state("A123")
    body = state.rsplit(".", 1)[0]
    forged_sig = sbd.sign_install_state("ATTACKER").rsplit(".", 1)[1]
    assert sbd.verify_install_state(f"{body}.{forged_sig}") is None


def test_state_rejects_expiry(monkeypatch):
    monkeypatch.setattr(sbd, "_STATE_MAX_AGE_SECONDS", 0)
    state = sbd.sign_install_state("A123")
    assert sbd.verify_install_state(state) is None


def test_state_fails_closed_with_no_secret_configured(monkeypatch):
    monkeypatch.delenv("COOLTON_WEB_SECRET", raising=False)
    state = sbd.sign_install_state("A123")
    assert sbd.verify_install_state(state) is None


# ---------------------------------------------------------------------------
# check_bot_install_status
# ---------------------------------------------------------------------------


def test_check_bot_install_status_unknown_uuid(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {})
    result = sbd.check_bot_install_status("nope")
    assert "unknown bot UUID" in result


def test_check_bot_install_status_not_yet_installed(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {"app123": {"app_id": "app123"}})
    result = sbd.check_bot_install_status("app123")
    assert result.startswith("not_installed")


def test_check_bot_install_status_installed(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {"app123": {"app_id": "app123", "bot_token": "xoxb-1"}})
    result = sbd.check_bot_install_status("app123")
    assert result.startswith("installed")


# ---------------------------------------------------------------------------
# get_bot_record — read-only lookup web/bot_oauth.py uses during the callback
# ---------------------------------------------------------------------------


def test_get_bot_record_returns_the_stored_record(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {"app123": {"app_id": "app123", "bot_token": "xoxb-1"}})
    assert sbd.get_bot_record("app123") == {"app_id": "app123", "bot_token": "xoxb-1"}


def test_get_bot_record_none_for_unknown_uuid(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {})
    assert sbd.get_bot_record("nope") is None


# ---------------------------------------------------------------------------
# create_slack_bot — apps.manifest.create takes no app_id (that's update-only)
# ---------------------------------------------------------------------------


def test_create_slack_bot_does_not_send_app_id_to_create(monkeypatch, tmp_path):
    monkeypatch.setattr(sbd, "STORE", tmp_path / "bots.json")
    calls = []

    def fake_api(method, data):
        calls.append((method, data))
        if method == "apps.manifest.validate":
            return {"ok": True}
        if method == "apps.manifest.create":
            return {"ok": True, "app_id": "A123", "credentials": {"signing_secret": "s"}}
        return {"ok": False, "error": "unexpected"}

    monkeypatch.setattr(sbd, "_api", fake_api)
    manifest = {"display_information": {"name": "Test Bot"}}
    result = sbd.create_slack_bot(manifest)

    assert "A123" in result
    create_call = next(c for c in calls if c[0] == "apps.manifest.create")
    assert "app_id" not in create_call[1]


def test_create_slack_bot_does_not_return_signing_secret(monkeypatch, tmp_path):
    """The signing secret must never reach the model's context — it's already
    persisted to disk (and wrangler_bot_deploy falls back to reading it from
    there), so returning it here would only risk it landing in a Slack message
    or conversation trace for no functional benefit."""
    store_path = tmp_path / "bots.json"
    monkeypatch.setattr(sbd, "STORE", store_path)

    def fake_api(method, data):
        if method == "apps.manifest.validate":
            return {"ok": True}
        if method == "apps.manifest.create":
            return {"ok": True, "app_id": "A123", "credentials": {"signing_secret": "very-secret"}}
        return {"ok": False, "error": "unexpected"}

    monkeypatch.setattr(sbd, "_api", fake_api)
    manifest = {"display_information": {"name": "Test Bot"}}
    result = sbd.create_slack_bot(manifest)

    assert "very-secret" not in result
    assert "signing_secret" not in result

    # ...but it's still persisted to disk for wrangler_bot_deploy to fall back to.
    stored = sbd._load()
    assert stored["A123"]["credentials"]["signing_secret"] == "very-secret"


def test_create_slack_bot_injects_the_oauth_callback_into_redirect_urls(monkeypatch, tmp_path):
    monkeypatch.setattr(sbd, "STORE", tmp_path / "bots.json")
    calls = []

    def fake_api(method, data):
        calls.append((method, data))
        if method == "apps.manifest.create":
            return {"ok": True, "app_id": "A123", "credentials": {"client_id": "cid"}}
        return {"ok": True}

    monkeypatch.setattr(sbd, "_api", fake_api)
    manifest = {"display_information": {"name": "Test Bot"}}
    sbd.create_slack_bot(manifest)

    for method, data in calls:
        assert sbd.oauth_callback_url() in data["manifest"]["oauth_config"]["redirect_urls"]
    # the caller's dict is never mutated in place
    assert "oauth_config" not in manifest


def test_create_slack_bot_preserves_existing_redirect_urls(monkeypatch, tmp_path):
    monkeypatch.setattr(sbd, "STORE", tmp_path / "bots.json")
    calls = []

    def fake_api(method, data):
        calls.append((method, data))
        if method == "apps.manifest.create":
            return {"ok": True, "app_id": "A123", "credentials": {"client_id": "cid"}}
        return {"ok": True}

    monkeypatch.setattr(sbd, "_api", fake_api)
    manifest = {
        "display_information": {"name": "Test Bot"},
        "oauth_config": {"redirect_urls": ["https://existing.example.com/callback"]},
    }
    sbd.create_slack_bot(manifest)

    sent = calls[0][1]["manifest"]["oauth_config"]["redirect_urls"]
    assert "https://existing.example.com/callback" in sent
    assert sbd.oauth_callback_url() in sent


def test_create_slack_bot_returns_an_authorize_url_with_redirect_and_state(monkeypatch, tmp_path):
    monkeypatch.setattr(sbd, "STORE", tmp_path / "bots.json")

    def fake_api(method, data):
        if method == "apps.manifest.create":
            return {"ok": True, "app_id": "A123", "credentials": {"client_id": "cid123"}}
        return {"ok": True}

    monkeypatch.setattr(sbd, "_api", fake_api)
    manifest = {
        "display_information": {"name": "Test Bot"},
        "oauth_config": {"scopes": {"bot": ["chat:write", "commands"]}},
    }
    result = json.loads(sbd.create_slack_bot(manifest))

    url = result["oauth_authorize_url"]
    assert result["auto_install"] is True
    assert "client_id=cid123" in url
    assert f"redirect_uri={quote(sbd.oauth_callback_url(), safe='')}" in url
    state = parse_qs(urlparse(url).query)["state"][0]
    assert sbd.verify_install_state(state) == "A123"


# ---------------------------------------------------------------------------
# update_slack_bot_manifest — apps.manifest.update, scoped to bots we created
# ---------------------------------------------------------------------------


def test_update_slack_bot_manifest_rejects_unknown_uuid(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {})
    result = sbd.update_slack_bot_manifest("unknown", {"display_information": {"name": "x"}})
    assert "unknown bot UUID" in result


def test_update_slack_bot_manifest_requires_display_name():
    result = sbd.update_slack_bot_manifest("app123", {"display_information": {}})
    assert "display_information.name is required" in result


def test_update_slack_bot_manifest_sends_app_id_to_both_calls(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {"app123": {"app_id": "app123"}})
    calls = []

    def fake_api(method, data):
        calls.append((method, data))
        return {"ok": True}

    monkeypatch.setattr(sbd, "_api", fake_api)
    manifest = {"display_information": {"name": "Test Bot"}}
    result = sbd.update_slack_bot_manifest("app123", manifest)

    assert "Manifest updated" in result
    methods = [c[0] for c in calls]
    assert methods == ["apps.manifest.validate", "apps.manifest.update"]
    for _, data in calls:
        assert data["app_id"] == "app123"
        assert data["manifest"] == manifest


def test_update_slack_bot_manifest_reports_validation_error(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {"app123": {"app_id": "app123"}})
    monkeypatch.setattr(sbd, "_api", lambda method, data: {"ok": False, "error": "invalid_manifest"})
    result = sbd.update_slack_bot_manifest("app123", {"display_information": {"name": "x"}})
    assert "invalid_manifest" in result


# ---------------------------------------------------------------------------
# wrangler_bot_deploy — secrets file must always be cleaned up, success or not
# ---------------------------------------------------------------------------


def test_wrangler_bot_deploy_missing_tokens_errors(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {})
    result = sbd.wrangler_bot_deploy("nope", "/work", "C1", "1.1")
    assert "bot token is not registered" in result


def test_wrangler_bot_deploy_works_without_app_token(monkeypatch):
    """HTTP-mode bots (no Socket Mode) never have an xapp- token — deploy must not
    require one, and must not write an empty SLACK_APP_TOKEN secret line."""
    monkeypatch.setattr(
        sbd, "_load", lambda: {"app123": {"app_id": "app123", "bot_token": "xoxb-1"}}
    )
    sandbox = Mock()
    sandbox.commands.run.return_value = Mock(stdout="deployed ok", stderr="", exit_code=0)
    monkeypatch.setattr(
        "agent.sandbox_helpers.get_or_create_sandbox", lambda c, t: (sandbox, {})
    )

    result = sbd.wrangler_bot_deploy("app123", "/work", "C1", "1.1")

    assert "deployed ok" in result
    _, write_content = sandbox.files.write.call_args.args
    assert "xoxb-1" in write_content
    assert "SLACK_APP_TOKEN" not in write_content


def test_wrangler_bot_deploy_writes_secrets_and_cleans_up_on_success(monkeypatch):
    _seed_bot(monkeypatch)
    sandbox = Mock()
    sandbox.commands.run.return_value = Mock(stdout="deployed ok", stderr="", exit_code=0)
    monkeypatch.setattr(
        "agent.sandbox_helpers.get_or_create_sandbox", lambda c, t: (sandbox, {})
    )

    result = sbd.wrangler_bot_deploy("app123", "/work", "C1", "1.1")

    assert "deployed ok" in result
    write_path, write_content = sandbox.files.write.call_args.args
    assert write_path == "/work/.env_slack"
    assert "xoxb-1" in write_content
    assert "xapp-1" in write_content

    cleanup_calls = [
        c for c in sandbox.commands.run.call_args_list if "rm -f" in c.args[0]
    ]
    assert len(cleanup_calls) == 1
    assert "/work/.env_slack" in cleanup_calls[0].args[0]


def test_wrangler_bot_deploy_cleans_up_secrets_even_when_deploy_raises(monkeypatch):
    _seed_bot(monkeypatch)
    sandbox = Mock()

    def run_side_effect(cmd, *a, **k):
        if "wrangler" in cmd:
            raise RuntimeError("sandbox died mid-deploy")
        return Mock(stdout="", stderr="", exit_code=0)

    sandbox.commands.run.side_effect = run_side_effect
    monkeypatch.setattr(
        "agent.sandbox_helpers.get_or_create_sandbox", lambda c, t: (sandbox, {})
    )

    result = sbd.wrangler_bot_deploy("app123", "/work", "C1", "1.1")

    assert "Error" in result
    assert "sandbox died mid-deploy" in result
    cleanup_calls = [
        c for c in sandbox.commands.run.call_args_list if "rm -f" in c.args[0]
    ]
    assert len(cleanup_calls) == 1, "secrets file must be cleaned up even when deploy fails"


def test_wrangler_bot_deploy_reports_nonzero_exit_code(monkeypatch):
    _seed_bot(monkeypatch)
    sandbox = Mock()
    sandbox.commands.run.return_value = Mock(stdout="", stderr="boom", exit_code=1)
    monkeypatch.setattr(
        "agent.sandbox_helpers.get_or_create_sandbox", lambda c, t: (sandbox, {})
    )

    result = sbd.wrangler_bot_deploy("app123", "/work", "C1", "1.1")

    assert "wrangler deploy failed (exit 1)" in result
    assert "boom" in result
