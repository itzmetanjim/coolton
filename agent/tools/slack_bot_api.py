import os
import requests


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
    
    url = f"https://slack.com/api/{method}"
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json; charset=utf-8"
    }
    
    try:
        response = requests.post(url, json=params, headers=headers, timeout=30)
        res_json = response.json()
        
        if res_json.get("ok"):
            return f"Success: {res_json}"
        
        return f"Slack API error: {res_json.get('error', 'unknown')}"
        
    except Exception as e:
        return f"Error executing Slack API call: {str(e)}"