import json


def _pt(text: str) -> dict:
    return {"type": "plain_text", "text": text}


def build_add_endpoint_modal() -> dict:
    return {
        "type": "modal",
        "callback_id": "byok_add_submit",
        "title": _pt("Add Endpoint"),
        "submit": _pt("Add"),
        "close": _pt("Cancel"),
        "blocks": [
            {"type": "header", "text": _pt("Add OpenAI-Compatible Endpoint")},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Any OpenAI-compatible API works: OpenAI, Anthropic (proxy), Groq, HCAI, OpenRouter, LMStudio, Ollama, vLLM, etc."}},
            {"type": "divider"},
            {"type": "input", "block_id": "ep_name", "label": _pt("Name"), "element": {"type": "plain_text_input", "action_id": "value", "placeholder": _pt("e.g. My OpenAI, My Groq, My HCAI")}},
            {"type": "input", "block_id": "ep_base_url", "label": _pt("API Base URL"), "element": {"type": "plain_text_input", "action_id": "value", "placeholder": _pt("e.g. https://api.openai.com/v1")}},
            {"type": "input", "block_id": "ep_api_key", "label": _pt("API Key"), "element": {"type": "plain_text_input", "action_id": "value", "placeholder": _pt("sk-...")}},
            {"type": "input", "block_id": "ep_model", "label": _pt("Model ID"), "element": {"type": "plain_text_input", "action_id": "value", "placeholder": _pt("e.g. gpt-4o, dall-e-3, llama-3.3-70b-versatile")}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "The model ID is what gets sent as the `model` parameter in API requests. Use the exact model name your provider expects."}},
        ],
    }


def build_edit_endpoint_modal(ep: dict) -> dict:
    return {
        "type": "modal",
        "callback_id": "byok_edit_submit",
        "private_metadata": json.dumps({"ep_id": ep["id"]}),
        "title": _pt("Edit Endpoint"),
        "submit": _pt("Save"),
        "close": _pt("Cancel"),
        "blocks": [
            {"type": "header", "text": _pt(f"Edit: {ep['name']}")},
            {"type": "divider"},
            {"type": "input", "block_id": "ep_name", "label": _pt("Name"), "element": {"type": "plain_text_input", "action_id": "value", "initial_value": ep["name"]}},
            {"type": "input", "block_id": "ep_base_url", "label": _pt("API Base URL"), "element": {"type": "plain_text_input", "action_id": "value", "initial_value": ep["base_url"]}},
            {"type": "input", "block_id": "ep_api_key", "label": _pt("API Key (leave blank to keep current)"), "element": {"type": "plain_text_input", "action_id": "value", "placeholder": _pt("Leave blank to keep current key")}, "optional": True},
            {"type": "input", "block_id": "ep_model", "label": _pt("Model ID"), "element": {"type": "plain_text_input", "action_id": "value", "initial_value": ep["model"]}},
        ],
    }


def handle_byok_add_submit(ack, body, client, context, logger):
    ack()
    try:
        from agent.byok_store import add_endpoint
        user_id = context.user_id
        values = body["view"]["state"]["values"]
        name = values["ep_name"]["value"]["value"]
        base_url = values["ep_base_url"]["value"]["value"]
        api_key = values["ep_api_key"]["value"]["value"]
        model = values["ep_model"]["value"]["value"]
        add_endpoint(user_id, name, base_url, api_key, model)
        client.chat_postEphemeral(channel=user_id, user=user_id, text=f"Added endpoint: {name} (`{model}`)")
    except Exception as e:
        logger.exception("Failed to add BYOK endpoint: %s", e)
        client.chat_postEphemeral(channel=user_id, user=user_id, text=f"Error adding endpoint: {str(e)}")


def handle_byok_edit_submit(ack, body, client, context, logger):
    ack()
    try:
        from agent.byok_store import update_endpoint
        user_id = context.user_id
        ep_id = json.loads(body["view"].get("private_metadata", "{}")).get("ep_id")
        if not ep_id:
            return
        values = body["view"]["state"]["values"]
        name = values["ep_name"]["value"]["value"]
        base_url = values["ep_base_url"]["value"]["value"]
        api_key = values["ep_api_key"]["value"].get("value", "")
        model = values["ep_model"]["value"]["value"]
        update_endpoint(user_id, ep_id, name, base_url, api_key, model)
        client.chat_postEphemeral(channel=user_id, user=user_id, text=f"Updated endpoint: {name} (`{model}`)")
    except Exception as e:
        logger.exception("Failed to update BYOK endpoint: %s", e)
        client.chat_postEphemeral(channel=user_id, user=user_id, text=f"Error updating endpoint: {str(e)}")
