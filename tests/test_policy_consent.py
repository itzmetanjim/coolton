import json

from agent import policy_consent as policy


def test_consent_roundtrip_and_revocation(tmp_path, monkeypatch):
    path = tmp_path / "consents.json"
    monkeypatch.setattr(policy, "_STORE_PATH", path)
    assert not policy.has_consent("U1")
    policy.record_consent("U1", joined_policy_channel=True)
    assert policy.has_consent("U1")
    assert json.loads(path.read_text())["consents"]["U1"]["joined_policy_channel"]
    policy.revoke_consent("U1")
    assert not policy.has_consent("U1")


def test_user_is_in_policy_channel_paginates_past_the_first_page():
    """conversations.members caps at 1000 per call; a channel with more members
    than that must not silently miss anyone past the first page."""
    from unittest.mock import Mock

    client = Mock()
    client.conversations_members.side_effect = [
        {"members": ["U_early"], "response_metadata": {"next_cursor": "page2"}},
        {"members": ["U_late"], "response_metadata": {"next_cursor": ""}},
    ]
    assert policy.user_is_in_policy_channel(client, "U_late")
    assert client.conversations_members.call_count == 2
    second_call_kwargs = client.conversations_members.call_args_list[1].kwargs
    assert second_call_kwargs["cursor"] == "page2"


def test_user_is_in_policy_channel_false_when_not_found_after_all_pages():
    from unittest.mock import Mock

    client = Mock()
    client.conversations_members.side_effect = [
        {"members": ["U_a"], "response_metadata": {"next_cursor": "page2"}},
        {"members": ["U_b"], "response_metadata": {"next_cursor": ""}},
    ]
    assert not policy.user_is_in_policy_channel(client, "U_absent")


def test_ensure_consent_skips_the_slack_api_call_when_already_recorded(tmp_path, monkeypatch):
    """ensure_consent runs on every single message; a user who already has
    consent recorded must not trigger a fresh conversations.members fetch every
    time — that's pure waste for the common case, and a real-time membership
    listener already keeps recorded consent in sync with channel membership."""
    from unittest.mock import Mock

    monkeypatch.setattr(policy, "_STORE_PATH", tmp_path / "consents.json")
    policy.record_consent("U1", joined_policy_channel=True)

    client = Mock()
    result = policy.ensure_consent(
        client, Mock(), user_id="U1", channel_id="C1", thread_ts="1.1", message_ts="1.1",
    )

    assert result is True
    client.conversations_members.assert_not_called()


def test_pending_request_is_single_use(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_STORE_PATH", tmp_path / "consents.json")
    pending_id = policy.save_pending({"user_id": "U1", "text": "hello"})
    assert policy.pop_pending(pending_id)["text"] == "hello"
    assert policy.pop_pending(pending_id) is None


def test_policy_buttons_have_requested_labels():
    blocks = policy.build_opt_in_blocks("pending")
    buttons = blocks[1]["elements"]
    assert buttons[0]["text"]["text"] == "opt in and join channel"
    assert buttons[0]["style"] == "primary"
    assert buttons[1]["text"]["text"] == "opt in without joining channel"
    assert "style" not in buttons[1]


def test_leaving_policy_channel_revokes_consent(monkeypatch):
    from unittest.mock import Mock
    from listeners.events.policy_membership import handle_member_left_channel

    record = Mock()
    monkeypatch.setattr("listeners.events.policy_membership.revoke_consent", record)
    logger = Mock()
    handle_member_left_channel(Mock(), {"channel": policy.POLICY_CHANNEL_ID, "user": "U1"}, logger)
    record.assert_called_once_with("U1")


def test_leaving_other_channel_does_not_revoke(monkeypatch):
    from unittest.mock import Mock
    from listeners.events.policy_membership import handle_member_left_channel

    record = Mock()
    monkeypatch.setattr("listeners.events.policy_membership.revoke_consent", record)
    handle_member_left_channel(Mock(), {"channel": "COTHER", "user": "U1"}, Mock())
    record.assert_not_called()


def test_joining_policy_channel_records_consent(monkeypatch):
    from unittest.mock import Mock
    from listeners.events.policy_membership import handle_member_joined_channel

    record = Mock()
    monkeypatch.setattr("listeners.events.policy_membership.record_consent", record)
    logger = Mock()
    handle_member_joined_channel(Mock(), {"channel": policy.POLICY_CHANNEL_ID, "user": "U1"}, logger)
    record.assert_called_once_with("U1", joined_policy_channel=True)


def test_joining_other_channel_does_not_record_consent(monkeypatch):
    from unittest.mock import Mock
    from listeners.events.policy_membership import handle_member_joined_channel

    record = Mock()
    monkeypatch.setattr("listeners.events.policy_membership.record_consent", record)
    handle_member_joined_channel(Mock(), {"channel": "COTHER", "user": "U1"}, Mock())
    record.assert_not_called()


def test_clear_pending_for_user_only_removes_that_users_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "_STORE_PATH", tmp_path / "consents.json")
    for user in ("U1", "U2"):
        policy.save_pending({"user_id": user, "text": "hello"})
    policy.clear_pending_for_user("U1")
    pending = json.loads((tmp_path / "consents.json").read_text())["pending"]
    assert pending
    assert all(p["user_id"] == "U2" for p in pending.values())
