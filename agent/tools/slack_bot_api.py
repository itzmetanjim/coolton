import os
import json
import requests

from agent.redact import redact, strip_secret_keys


def slack_api_call_as_bot(method: str, params: dict) -> str:
    """Make an arbitrary Slack API call as the BOT (not cooltonUser).
    
    Uses SLACK_BOT_TOKEN instead of SLACK_USER_TOKEN.
    Use this for bot-level actions like posting messages as the bot, 
    updating bot messages, managing bot's own reactions, etc.
    
    Args:
        method: Slack API method (e.g., 'chat.postMessage', 'chat.update', 'reactions.add').
        params: Dictionary of parameters for the method.
        
    Returns:
        Success/error message.
    """
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        return "Error: SLACK_BOT_TOKEN not configured"
    if method.startswith("apps.manifest."):
        return f"Error: {method} requires a Slack App Configuration Token (xoxe), not a bot token. Use the create_slack_bot tool instead, or set SLACK_CONFIG_TOKEN."
    if not params:
        required_hint = {
            "conversations.history": "'channel'",
            "conversations.replies": "'channel' and 'ts'",
            "conversations.open": "'users' or 'channel'",
            "conversations.list": "'team_id' in this enterprise workspace",
        }.get(method, "the method's required parameters")
        return f"Error: {method} requires params; pass {required_hint}, not an empty object."
    if method == "chat.postMessage":
        if not params.get("channel"):
            return "Error: chat.postMessage requires a 'channel' (channel id or user id for a DM) param — use the chat_postMessage tool instead."
        if not params.get("text"):
            return "Error: chat.postMessage requires a 'text' param — use the chat_postMessage tool instead."
    
    url = f"https://slack.com/api/{method}"
    headers = {
        "Authorization": f"Bearer {bot_token}"
    }
    form = {
        k: json.dumps(v) if isinstance(v, (dict, list)) else v
        for k, v in params.items()
    }
    
    try:
        response = requests.post(url, data=form, headers=headers, timeout=30)
        res_json = strip_secret_keys(response.json())
        
        if res_json.get("ok"):
            return f"Success: {redact(str(res_json), context='slack_api_call_as_bot')}"
        
        return f"Slack API error: {redact(str(res_json), context='slack_api_call_as_bot')}"
        
    except Exception as e:
        return f"Error executing Slack API call: {redact(str(e), context='slack_api_call_as_bot')}"