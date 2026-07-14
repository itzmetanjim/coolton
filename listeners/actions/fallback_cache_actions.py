import logging

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

logger = logging.getLogger(__name__)


def handle_fallback_cache_clear(ack: Ack, client: WebClient, context: BoltContext):
    ack()
    try:
        from agent.fallback_cache import clear_cache
        clear_cache(context.user_id)
        client.chat_postEphemeral(
            channel=context.user_id,
            user=context.user_id,
            text="Fallback cache cleared. The bot will go through the full provider chain on next request."
        )
    except Exception as e:
        logger.exception("Failed to clear fallback cache: %s", e)
