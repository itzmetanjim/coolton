"""Who counts as "coolton" when someone @mentions her, and answering each mention once.

coolton is reachable under two bot users: her own (COOLTON_BOT_ID), and the
"coolton code channels" app's bot (CODE_CHANNEL_BOT_ID, the agent of every code
channel; listeners.code_channel_app). A mention of either is a mention of coolton.
Several listeners can see the same mention (coolton's app_mention and message events,
and the code channels app's app_mention), so the first to claim it answers it
(claim_mention) and the rest drop it.
"""
import os
import re
import threading
import time

CODE_CHANNEL_BOT_ID = os.environ.get("COOLTON_CODE_CHANNEL_BOT_ID", "U0C848QS7GU")

_claimed: dict[tuple[str, str], float] = {}
_lock = threading.Lock()
_CLAIM_TTL_SECONDS = 600


def mentions_code_channel_bot(text: str) -> bool:
    return f"<@{CODE_CHANNEL_BOT_ID}" in (text or "")


def as_coolton_mention(text: str) -> str:
    """`text` with mentions of the code channels bot turned into mentions of coolton,
    only for recognizing commands (!stop, !help...): the model always gets the message
    as written, and knows from its prompt that the code channels bot is her."""
    bot_id = os.environ.get("COOLTON_BOT_ID", "")
    if not bot_id:
        return text
    return re.sub(rf"<@{CODE_CHANNEL_BOT_ID}(\|[^>]*)?>", f"<@{bot_id}>", text or "")


def claim_mention(channel_id: str, ts: str) -> bool:
    """True the first time a message is claimed (answer it), False after (drop it)."""
    key, now = (channel_id, ts), time.time()
    with _lock:
        for old in [k for k, t in _claimed.items() if now - t > _CLAIM_TTL_SECONDS]:
            del _claimed[old]
        if key in _claimed:
            return False
        _claimed[key] = now
        return True
