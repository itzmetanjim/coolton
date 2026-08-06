import os


def ensure_coolton_user_in_channel(client, channel_id: str) -> None:
    """Silently ensure cooltonUser is a member of the channel.

    Called whenever coolton is mentioned. Best-effort membership check; if the
    check can't run (e.g. missing channels:read/groups:read) we still invite,
    treating `already_in_channel` (or any failure) as success. Purely a silent
    side-effect: no logging, no Slack message, no errors.
    """
    coolton_user_id = os.environ.get("COOLTON_USER_ID")
    if not coolton_user_id or not channel_id:
        return
    try:
        if _is_member(client, channel_id, coolton_user_id):
            return
        client.conversations_invite(channel=channel_id, users=coolton_user_id)
    except Exception:
        pass


def _is_member(client, channel_id: str, coolton_user_id: str) -> bool:
    try:
        cursor = None
        while True:
            resp = client.conversations_members(channel=channel_id, cursor=cursor)
            if not resp.get("ok"):
                return False
            members = resp.get("members", [])
            if coolton_user_id in members:
                return True
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return False
    except Exception:
        return False
