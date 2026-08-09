from unittest.mock import Mock

from listeners.views.instructions_views import (
    build_instructions_modal,
    handle_instructions_submit,
)


def _input(modal):
    return next(b for b in modal["blocks"] if b["type"] == "input")


def test_build_instructions_modal_default():
    modal = build_instructions_modal()
    assert modal["type"] == "modal"
    assert modal["callback_id"] == "custom_instructions_submit"
    assert modal["submit"]["text"] == "Save"
    assert modal["close"]["text"] == "Cancel"

    field = _input(modal)
    assert field["block_id"] == "instructions"
    assert field["element"]["type"] == "plain_text_input"
    assert field["element"]["multiline"] is True
    assert field["element"].get("initial_value", "") == ""
    assert field["optional"] is True


def test_build_instructions_modal_prefilled():
    modal = build_instructions_modal("always use lowercase")
    assert _input(modal)["element"]["initial_value"] == "always use lowercase"


def test_handle_instructions_submit_saves(monkeypatch):
    ack = Mock()
    client = Mock()
    context = Mock(user_id="U123")
    logger = Mock()
    body = {
        "view": {
            "state": {
                "values": {"instructions": {"value": {"value": "be concise"}}}
            }
        }
    }

    set_user_instructions = Mock()
    monkeypatch.setattr(
        "listeners.actions.instructions_actions.set_user_instructions",
        set_user_instructions,
    )

    handle_instructions_submit(ack, body, client, context, logger)

    ack.assert_called_once()
    set_user_instructions.assert_called_once_with("U123", "be concise")
    client.chat_postEphemeral.assert_called_once_with(
        channel="U123", user="U123", text="Your custom instructions have been saved."
    )


def test_handle_instructions_submit_clears_on_empty(monkeypatch):
    ack = Mock()
    client = Mock()
    context = Mock(user_id="U123")
    logger = Mock()
    body = {"view": {"state": {"values": {"instructions": {"value": {"value": ""}}}}}}

    set_user_instructions = Mock()
    monkeypatch.setattr(
        "listeners.actions.instructions_actions.set_user_instructions",
        set_user_instructions,
    )

    handle_instructions_submit(ack, body, client, context, logger)

    set_user_instructions.assert_called_once_with("U123", "")
    client.chat_postEphemeral.assert_called_once_with(
        channel="U123", user="U123", text="Your custom instructions have been cleared."
    )


def test_handle_instructions_submit_error(monkeypatch):
    ack = Mock()
    client = Mock()
    context = Mock(user_id="U123")
    logger = Mock()
    body = {"view": {"state": {"values": {}}}}

    monkeypatch.setattr(
        "listeners.actions.instructions_actions.set_user_instructions",
        Mock(side_effect=RuntimeError("boom")),
    )

    handle_instructions_submit(ack, body, client, context, logger)
    logger.exception.assert_called()
