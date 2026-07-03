from agent.leave_thread_store import leave_thread, rejoin_thread, is_thread_left, should_ignore_thread


def leave_thread_tool(channel_id: str, thread_ts: str) -> str:
    """Leave the current thread - bot will ignore all future messages in this thread until @mentioned again.
    
    Use this when you want to stop responding in a thread but still want to be available if mentioned.
    
    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp to leave.
        
    Returns:
        Confirmation message.
    """
    return leave_thread(channel_id, thread_ts)


def rejoin_thread_tool(channel_id: str, thread_ts: str) -> str:
    """Rejoin a thread that was previously left (happens automatically on @mention).
    
    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp to rejoin.
        
    Returns:
        Confirmation message.
    """
    from agent.leave_thread_store import rejoin_thread
    return rejoin_thread(channel_id, thread_ts)