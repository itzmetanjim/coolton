from unittest.mock import Mock

from agent.tools import slack_bot_deploy as sbd


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
    assert "only xoxb- bot tokens and xapp- app tokens are accepted" in result


def test_register_bot_tokens_rejects_unknown_uuid(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {})
    result = sbd.register_bot_tokens("unknown", "xoxb-1", "xapp-1")
    assert "unknown bot UUID" in result


# ---------------------------------------------------------------------------
# wrangler_bot_deploy — secrets file must always be cleaned up, success or not
# ---------------------------------------------------------------------------


def test_wrangler_bot_deploy_missing_tokens_errors(monkeypatch):
    monkeypatch.setattr(sbd, "_load", lambda: {})
    result = sbd.wrangler_bot_deploy("nope", "/work", "C1", "1.1")
    assert "not registered" in result


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
