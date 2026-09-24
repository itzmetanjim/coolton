import logging

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

from agent import provider_config
from agent.admin_alerts import ADMIN_USER_ID
from agent.provider_probe import probe_all
from listeners.actions.byok_actions import _notify_modal_failure
from listeners.events.turn import _chunk_text

logger = logging.getLogger(__name__)


def _build_provider_order(user_id: str) -> list[tuple[str, dict]]:
    return provider_config.build_provider_order(user_id)


# Provider tests spend real model calls on every configured provider, so only
# the maintainer (agent.admin_alerts.ADMIN_USER_ID) can run them — the App
# Home only shows the buttons to them, and each handler re-checks, since an
# old Home view (or a crafted action payload) can still carry the button.
_NOT_ALLOWED_TEXT = "Only the coolton maintainer can run provider tests."


def _refuse_unless_admin(client: WebClient, user_id: str | None) -> bool:
    """True (after telling the user why) if `user_id` may not run provider tests."""
    if user_id == ADMIN_USER_ID:
        return False
    if user_id:
        try:
            client.chat_postEphemeral(channel=user_id, user=user_id, text=_NOT_ALLOWED_TEXT)
        except Exception:
            logger.warning("Couldn't tell %s provider tests are maintainer-only", user_id)
    return True


def handle_test_providers(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        user_id = context.user_id
        if _refuse_unless_admin(client, user_id):
            return
        _probe_and_report(client, user_id, _build_provider_order(user_id), "Testing all AI providers...")
    except Exception as e:
        logger.exception("Failed to test providers: %s", e)


def _probe_and_report(client: WebClient, user_id: str, order: list[tuple[str, dict]], header_text: str) -> None:
    """Shared by "Test All Providers" and "Test a Provider" (the latter passes
    a one-entry `order`), so both report exactly the same way."""
    # A real (non-ephemeral) top-level message, so it has a genuine `ts` the
    # rest of the run can thread off of — an ephemeral message can't anchor
    # a thread. Everything after this — the "may take a minute" notice and
    # every result chunk — replies into that one thread instead of posting
    # as its own separate top-level message (which is what happened before:
    # one un-chunked chat.postMessage over Slack's per-message char limit
    # came back as several disconnected top-level posts, each looking like
    # the start of a new conversation).
    header = client.chat_postMessage(channel=user_id, text=header_text)
    thread_ts = header.get("ts")
    client.chat_postMessage(channel=user_id, thread_ts=thread_ts, text="(this may take a minute)")

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


# Slack caps a static_select at 100 options and each option's text at 75 chars.
_MAX_OPTIONS = 100
_MAX_OPTION_TEXT = 75


def _option_label(provider_name: str, config: dict) -> str:
    label = f"{provider_name} / {config.get('model', '?')}"
    return label if len(label) <= _MAX_OPTION_TEXT else label[: _MAX_OPTION_TEXT - 1] + "…"


def build_test_provider_modal(order: list[tuple[str, dict]]) -> dict:
    options = [
        {"text": {"type": "plain_text", "text": _option_label(name, config)}, "value": name}
        for name, config in order[:_MAX_OPTIONS]
    ]
    return {
        "type": "modal",
        "callback_id": "test_provider_submit",
        "title": {"type": "plain_text", "text": "Test a Provider"},
        "submit": {"type": "plain_text", "text": "Test"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "provider",
                "label": {"type": "plain_text", "text": "Provider"},
                "element": {
                    "type": "static_select",
                    "action_id": "value",
                    "placeholder": {"type": "plain_text", "text": "Pick a provider to test"},
                    "options": options,
                },
            },
        ],
    }


def handle_test_provider_open(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    user_id = context.user_id
    if _refuse_unless_admin(client, user_id):
        return
    try:
        order = _build_provider_order(user_id)
        if not order:
            client.chat_postEphemeral(channel=user_id, user=user_id, text="No AI providers configured.")
            return
        client.views_open(trigger_id=body["trigger_id"], view=build_test_provider_modal(order))
    except Exception as e:
        logger.exception("Failed to open Test a Provider modal: %s", e)
        _notify_modal_failure(client, user_id)


def handle_test_provider_submit(ack: Ack, body: dict, client: WebClient, view: dict):
    ack()
    try:
        user_id = body["user"]["id"]
        if _refuse_unless_admin(client, user_id):
            return
        selected = view["state"]["values"]["provider"]["value"]["selected_option"]["value"]
        # Re-resolved at submit time rather than trusting anything carried in
        # the modal: the config (API key included) is never put in the view.
        order = [entry for entry in _build_provider_order(user_id) if entry[0] == selected]
        if not order:
            client.chat_postMessage(channel=user_id, text=f"`{selected}` is no longer a configured provider.")
            return
        _probe_and_report(client, user_id, order, f"Testing `{selected}`...")
    except Exception as e:
        logger.exception("Failed to test provider: %s", e)
