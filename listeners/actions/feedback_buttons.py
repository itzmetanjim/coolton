from logging import Logger

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

from listeners.views.feedback_views import build_feedback_modal


def handle_feedback_button(
    ack: Ack, body: dict, client: WebClient, context: BoltContext, logger: Logger
):
    """Thumbs up/down on a response opens a modal for an optional comment,
    then DMs the admin the feedback plus a link to the message."""
    ack()

    try:
        channel_id = context.channel_id
        message_ts = body["message"]["ts"]
        feedback_value = body["actions"][0]["value"]
        modal = build_feedback_modal(feedback_value, channel_id, message_ts)
        client.views_open(trigger_id=body["trigger_id"], view=modal)
    except Exception as e:
        logger.exception(f"Failed to open feedback modal: {e}")
        try:
            client.chat_postEphemeral(
                channel=context.channel_id,
                user=context.user_id,
                thread_ts=body.get("message", {}).get("ts"),
                text="Couldn't open the feedback form — please try again.",
            )
        except Exception:
            pass
