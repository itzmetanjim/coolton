import logging

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

from agent import provider_config
from agent.provider_probe import probe_all

logger = logging.getLogger(__name__)


def _build_provider_order(user_id: str) -> list[tuple[str, dict]]:
    return provider_config.build_provider_order(user_id)


def handle_test_providers(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        user_id = context.user_id
        client.chat_postEphemeral(
            channel=user_id, user=user_id,
            text="Testing all AI providers... this may take a minute.",
        )

        order = _build_provider_order(user_id)
        if not order:
            client.chat_postMessage(channel=user_id, text="No AI providers configured.")
            return

        # Probes run in parallel across providers (serially within one provider,
        # to not hammer a single upstream's rate limits) — a fully sequential
        # sweep of every configured model took roughly the sum of all of their
        # latencies, tens of seconds to minutes.
        results = []
        for provider_name, ok, display, elapsed, detail in probe_all(order):
            status = ":white_check_mark:" if ok else ":x:"
            line = f"{status} *{display}* — {elapsed:.1f}s"
            if ok:
                line += f"\n       {detail}"
            else:
                line += f"\n       ```\n{detail}\n       ```"
            results.append(line)

        lines = "\n".join(results)
        client.chat_postMessage(
            channel=user_id,
            text=f"*AI Provider Test Results*\n{lines}",
            mrkdwn=True,
        )
    except Exception as e:
        logger.exception("Failed to test providers: %s", e)
