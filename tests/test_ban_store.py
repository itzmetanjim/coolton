import json

import pytest

from agent import ban_store as store


@pytest.fixture
def tmp_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "BAN_STORE_FILE", str(tmp_path / "ban_store.json"))
    return tmp_path


def test_unbanned_user_by_default(tmp_store):
    assert store.is_banned("U1") is False


def test_ban_then_is_banned(tmp_store):
    store.ban_user("U1", reason="spamming")
    assert store.is_banned("U1") is True


def test_unban_then_not_banned(tmp_store):
    store.ban_user("U1")
    store.unban_user("U1")
    assert store.is_banned("U1") is False


def test_users_are_independent(tmp_store):
    store.ban_user("U1")
    assert store.is_banned("U1") is True
    assert store.is_banned("U2") is False


def test_corrupt_file_falls_back_to_unbanned(tmp_store):
    (tmp_store / "ban_store.json").write_text("{nope")
    assert store.is_banned("U1") is False


def test_non_dict_top_level_json_falls_back_to_unbanned(tmp_store):
    """A hand-edited or otherwise corrupted file whose top level parses as
    valid JSON but isn't an object (e.g. a bare list) must not raise
    AttributeError the next time something calls .get() on it."""
    (tmp_store / "ban_store.json").write_text("[]")
    assert store.is_banned("U1") is False


def test_non_dict_entry_for_a_user_falls_back_to_unbanned(tmp_store):
    (tmp_store / "ban_store.json").write_text('{"U1": true}')
    assert store.is_banned("U1") is False


def test_persists_to_disk(tmp_store):
    store.ban_user("U1", reason="spamming")
    data = json.loads((tmp_store / "ban_store.json").read_text())
    assert data["U1"]["banned"] is True
    assert data["U1"]["reason"] == "spamming"


def test_is_authorized_only_the_hardcoded_admin():
    assert store.is_authorized(store.BAN_ADMIN_USER_ID) is True
    assert store.is_authorized("U_SOMEONE_ELSE") is False


class TestParseBanCommand:
    def test_plain_ban_with_reason(self):
        assert store.parse_ban_command("!ban <@U123> being annoying") == ("ban", "U123", "being annoying")

    def test_plain_unban_with_reason(self):
        assert store.parse_ban_command("!unban <@U123> appeal accepted") == ("unban", "U123", "appeal accepted")

    def test_no_reason(self):
        assert store.parse_ban_command("!ban <@U123>") == ("ban", "U123", "")

    def test_mention_with_no_space_before_bang(self):
        """The user's own example syntax: "<@BOT>!ban <@U...> reason", no space
        between the bot mention and the "!"."""
        assert store.parse_ban_command("<@UBOT>!ban <@U123> reason", bot_id="UBOT") == ("ban", "U123", "reason")

    def test_mention_with_space_before_bang(self):
        assert store.parse_ban_command("<@UBOT> !ban <@U123> reason", bot_id="UBOT") == ("ban", "U123", "reason")

    def test_target_mention_with_pipe_label_is_stripped_to_the_id(self):
        """Slack renders some user mentions as "<@U123|display name>", not
        just the bare "<@U123>" form — the id alone must still be extracted."""
        assert store.parse_ban_command("!ban <@U123|some.user> being annoying") == ("ban", "U123", "being annoying")

    def test_target_mention_with_pipe_label_and_bot_prefix(self):
        assert store.parse_ban_command(
            "<@UBOT>!ban <@U123|some.user> reason", bot_id="UBOT",
        ) == ("ban", "U123", "reason")

    def test_case_insensitive_command_word(self):
        assert store.parse_ban_command("!BAN <@U123>") == ("ban", "U123", "")

    def test_not_a_ban_command_returns_none(self):
        assert store.parse_ban_command("hey can you ban <@U123>?") is None
        assert store.parse_ban_command("!ban") is None
        assert store.parse_ban_command("!banned <@U123>") is None

    def test_wrong_bot_mention_prefix_is_not_stripped(self):
        """A mention of a DIFFERENT bot/user isn't the addressed mention — it stays
        part of the text, so this must not parse as a bare "!ban ...", and since it
        isn't one either (it's some other mention followed by !ban with no target
        right after it) it should fail to match."""
        assert store.parse_ban_command("<@USOMEONEELSE>!ban <@U123>", bot_id="UBOT") is None


def test_format_announcement_ban_with_reason():
    text = store.format_announcement("ban", "U123", "being annoying")
    assert text == (
        "*A user has been banned from Coolton.*\n\n"
        "*User:* <@U123>\n"
        "*Reason:* being annoying"
    )


def test_format_announcement_ban_without_reason_omits_reason_line():
    text = store.format_announcement("ban", "U123", "")
    assert text == "*A user has been banned from Coolton.*\n\n*User:* <@U123>"
    assert "Reason" not in text


def test_format_announcement_unban():
    text = store.format_announcement("unban", "U123", "")
    assert text.startswith("*A user has been unbanned from Coolton.*")


def test_apply_ban_command_bans_and_announces(tmp_store):
    from unittest.mock import Mock

    client = Mock()
    store.apply_ban_command(client, "ban", "U123", "being annoying")
    assert store.is_banned("U123") is True
    client.chat_postMessage.assert_called_once()
    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == store.ANNOUNCE_CHANNEL_ID
    assert "banned" in kwargs["text"]
    assert "U123" in kwargs["text"]


def test_apply_ban_command_unbans_and_announces(tmp_store):
    from unittest.mock import Mock

    store.ban_user("U123")
    client = Mock()
    store.apply_ban_command(client, "unban", "U123", "")
    assert store.is_banned("U123") is False
    kwargs = client.chat_postMessage.call_args.kwargs
    assert "unbanned" in kwargs["text"]
