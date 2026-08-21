from unittest.mock import Mock

from listeners.actions.instructions_actions import handle_instructions_open


def test_open_modal_notifies_user_when_views_open_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "listeners.actions.instructions_actions.INSTRUCTIONS_FILE",
        str(tmp_path / "custom_instructions.json"),
    )
    client = Mock()
    client.views_open.side_effect = RuntimeError("trigger_id expired")
    ack = Mock()
    context = Mock()
    context.user_id = "U1"

    handle_instructions_open(ack, {"trigger_id": "t1"}, client, context)

    ack.assert_called_once()
    client.chat_postEphemeral.assert_called_once()
    kwargs = client.chat_postEphemeral.call_args.kwargs
    assert kwargs["channel"] == "U1"
    assert "try again" in kwargs["text"]
