import logging

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

from agent import provider_config
from agent.provider_probe import probe_all
from listeners.events.turn import _chunk_text

logger = logging.getLogger(__name__)


def _build_provider_order(user_id: str) -> list[tuple[str, dict]]:
    return provider_config.build_provider_order(user_id)


def handle_test_providers(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        user_id = context.user_id

        # A real (non-ephemeral) top-level message, so it has a genuine `ts` the
        # rest of the run can thread off of — an ephemeral message can't anchor
        # a thread. Everything after this — the "may take a minute" notice and
        # every result chunk — replies into that one thread instead of posting
        # as its own separate top-level message (which is what happened before:
        # one un-chunked chat.postMessage over Slack's per-message char limit
        # came back as several disconnected top-level posts, each looking like
        # the start of a new conversation).
        header = client.chat_postMessage(channel=user_id, text="Testing all AI providers...")
        thread_ts = header.get("ts")
        client.chat_postMessage(channel=user_id, thread_ts=thread_ts, text="(this may take a minute)")

        order = _build_provider_order(user_id)
        if not order:
            client.chat_postMessage(channel=user_id, thread_ts=thread_ts, text="No AI providers configured.")
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

        text = "*AI Provider Test Results*\n" + "\n".join(results)
        for chunk in _chunk_text(text):
            client.chat_postMessage(channel=user_id, thread_ts=thread_ts, text=chunk, mrkdwn=True)
    except Exception as e:
        logger.exception("Failed to test providers: %s", e)
