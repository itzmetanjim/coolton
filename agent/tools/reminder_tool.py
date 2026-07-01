from agent.scheduler import schedule_reminder


def schedule_reminder_tool(user_id: str, channel_id: str, text: str, delay_seconds: int) -> str:
    """Schedule a one-time reminder that will be DM'd to the user.

    Args:
        user_id: Slack user ID to send the reminder to.
        channel_id: Channel context where the reminder was set.
        text: Reminder message text.
        delay_seconds: Seconds from now until the reminder fires (max 120 days = 10368000s).

    Returns:
        Confirmation or error message.
    """
    if delay_seconds <= 0:
        return "Error: delay_seconds must be positive."
    if delay_seconds > 10368000:
        return "Error: delay_seconds exceeds maximum of 120 days (10368000 seconds)."

    reminder_id = schedule_reminder(user_id, channel_id, text, delay_seconds)
    return f"Reminder set (`#{reminder_id}`). I'll DM you in {_format_duration(delay_seconds)}."
     

def _format_duration(seconds: int) -> str:
    parts = []
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)
