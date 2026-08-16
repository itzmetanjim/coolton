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
