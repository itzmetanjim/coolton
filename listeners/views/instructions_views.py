def _pt(text: str) -> dict:
    return {"type": "plain_text", "text": text}


def build_instructions_modal(current_instructions: str = "") -> dict:
    return {
        "type": "modal",
        "callback_id": "custom_instructions_submit",
        "title": _pt("Custom Instructions"),
        "submit": _pt("Save"),
        "close": _pt("Cancel"),
        "blocks": [
            {"type": "header", "text": _pt("Custom Instructions")},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Tell coolton how you want to be treated. These instructions are injected into every conversation."}},
            {"type": "divider"},
            {
                "type": "input",
                "block_id": "instructions",
                "label": _pt("Your custom instructions"),
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "placeholder": _pt("e.g. Be extra concise. Use emoji liberally. Never mention gorkie."),
                    "multiline": True,
                    "max_length": 3000,
                    "initial_value": current_instructions,
                },
                "optional": True,
            },
            {"type": "section", "text": {"type": "mrkdwn", "text": "These apply *only to you* and persist across restarts."}},
        ],
    }


def handle_instructions_submit(ack, body, client, context, logger):
    ack()
    try:
        from listeners.actions.instructions_actions import set_user_instructions
        user_id = context.user_id
        value = body["view"]["state"]["values"].get("instructions", {}).get("value", {}).get("value", "")
        set_user_instructions(user_id, value)
        client.chat_postEphemeral(
            channel=user_id,
            user=user_id,
            text="Your custom instructions have been saved."
            if value.strip()
            else "Your custom instructions have been cleared.",
        )
    except Exception as e:
        logger.exception("Failed to save instructions: %s", e)
