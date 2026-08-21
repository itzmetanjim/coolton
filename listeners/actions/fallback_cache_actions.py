import logging
import threading

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

logger = logging.getLogger(__name__)


def handle_fallback_cache_clear(ack: Ack, client: WebClient, context: BoltContext):
    ack()
    try:
        from agent.fallback_cache import clear_cache
        clear_cache()
        client.chat_postEphemeral(
            channel=context.user_id,
            user=context.user_id,
            text="Fallback cache cleared (global). Re-probing all providers in the background now — "
                 "should be back up to date within a minute or so.",
        )
        # Don't make the user wait up to REFRESH_INTERVAL_SECONDS for the next
        # scheduled cycle, and don't block this handler on a probe that can
        # take up to ~a minute across every configured provider.
        threading.Thread(target=_refresh_now, name="fallback-cache-manual-refresh", daemon=True).start()
    except Exception as e:
        logger.exception("Failed to clear fallback cache: %s", e)


def _refresh_now():
    try:
        from agent.provider_probe import refresh_fallback_cache
        refresh_fallback_cache()
    except Exception:
        logger.exception("Manual fallback cache refresh (after clear) failed")
