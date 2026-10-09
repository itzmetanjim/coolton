"""listeners.code_channel_app and agent.mention_ids: a mention of the code channels bot
is answered by coolton, once, with the message as written."""
from unittest.mock import Mock

import pytest

from agent import mention_ids
from listeners import code_channel_app


@pytest.fixture(autouse=True)
def ids(monkeypatch):
    monkeypatch.setenv("COOLTON_BOT_ID", "UCOOLTON")
    monkeypatch.setenv("COOLTON_USER_ID", "UHELPER")


def test_a_mention_of_the_code_channel_bot_adds_coolton_registers_the_channel_and_is_answered(monkeypatch):
    registered, handled = [], []
    monkeypatch.setattr("agent.code_channel_store.is_code_channel", lambda c: False)
    monkeypatch.setattr("agent.code_channel_store.register_code_channel", lambda *a: registered.append(a))
    monkeypatch.setattr("listeners.events.app_mentioned.handle_app_mentioned", lambda **kw: handled.append(kw))
    app_client, coolton = Mock(), Mock()
    coolton.conversations_info.return_value = {"channel": {"name": "test", "properties": {
        "record_channel": {"record_type": "agent_channel"}}}}
    text = f"<@{mention_ids.CODE_CHANNEL_BOT_ID}> hihi test"

    code_channel_app.handle_code_channel_app_mention(
        {"channel": "C7", "user": "U1", "ts": "1.1", "text": text, "team": "T1"}, app_client, coolton)

    app_client.conversations_invite.assert_called_once_with(channel="C7", users="UCOOLTON,UHELPER", force=True)
    assert registered == [("C7", "test", "U1", "C7", "")]
    [kw] = handled
    assert kw["client"] is coolton and kw["event"]["text"] == text  # coolton sees which bot was mentioned


def test_each_mention_is_answered_once():
    assert mention_ids.claim_mention("C1", "1.1") is True
    assert mention_ids.claim_mention("C1", "1.1") is False
    assert mention_ids.claim_mention("C1", "1.2") is True


def test_commands_work_after_a_mention_of_either_bot():
    assert mention_ids.as_coolton_mention(f"<@{mention_ids.CODE_CHANNEL_BOT_ID}> !stop") == "<@UCOOLTON> !stop"
