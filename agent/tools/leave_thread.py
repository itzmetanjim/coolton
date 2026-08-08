from agent.leave_thread_store import leave_thread, join_thread


def leave_thread_tool(channel_id: str, thread_ts: str) -> str:
    """Leave the current thread - bot will ignore all future messages in this thread until mentioned again.

    Use this when you want to stop responding in a thread but still want to be available if mentioned.

    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp to leave.

    Returns:
        Confirmation message.
    """
    return leave_thread(channel_id, thread_ts)


def join_thread_tool(channel_id: str, thread_ts: str) -> str:
    """Join the current thread - respond to every message here until told to leave.

    Use this when the user asks you to stay in (or keep responding in) a thread
    whose starter message didn't mention you.

    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp to join.

    Returns:
        Confirmation message.
    """
    return join_thread(channel_id, thread_ts)
