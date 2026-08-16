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
