import os


def ensure_coolton_user_in_channel(client, channel_id: str) -> None:
    """Silently ensure cooltonUser is a member of the channel.

    Called whenever coolton is mentioned. The bot token has no
    channels:read/groups:read, so membership can't be pre-checked — we just
    invite and treat `already_in_channel` (or any other failure) as success.
    Purely a silent side-effect: no logging, no Slack message, no errors.
    """
    coolton_user_id = os.environ.get("COOLTON_USER_ID")
    if not coolton_user_id or not channel_id:
        return
    try:
        client.conversations_invite(channel=channel_id, users=coolton_user_id)
    except Exception:
        pass
