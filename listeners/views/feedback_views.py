import json
import logging

from agent.admin_alerts import notify_admin

logger = logging.getLogger(__name__)


def _pt(text: str) -> dict:
    return {"type": "plain_text", "text": text}


def build_feedback_modal(feedback_value: str, channel_id: str, message_ts: str) -> dict:
    is_positive = feedback_value == "good-feedback"
    return {
        "type": "modal",
        "callback_id": "feedback_submit",
        "private_metadata": json.dumps({
            "channel_id": channel_id,
            "message_ts": message_ts,
            "feedback_value": feedback_value,
        }),
        "title": _pt("Good response?" if is_positive else "What went wrong?"),
        "submit": _pt("Send"),
        "close": _pt("Skip"),
        "blocks": [
            {
                "type": "input",
                "block_id": "comment",
                "optional": True,
                "label": _pt("Anything you want to add? (optional)"),
                "element": {
                    "type": "plain_text_input",
                    "action_id": "value",
                    "multiline": True,
                    "max_length": 1000,
                    "placeholder": _pt(
                        "What made this response good."
                        if is_positive
                        else "What made this response bad, or what you expected instead."
                    ),
                },
            },
        ],
    }


def handle_feedback_submit(ack, body, client, context, logger):
    ack()
    try:
        metadata = json.loads(body["view"]["private_metadata"])
        channel_id = metadata["channel_id"]
        message_ts = metadata["message_ts"]
        feedback_value = metadata["feedback_value"]
        user_id = context.user_id
        comment = (
            body["view"]["state"]["values"]
            .get("comment", {})
            .get("value", {})
            .get("value")
            or ""
        ).strip()

        link = ""
        try:
            link_resp = client.chat_getPermalink(channel=channel_id, message_ts=message_ts)
            if link_resp.get("ok"):
                link = link_resp.get("permalink", "")
        except Exception:
            logger.exception("Failed to fetch permalink for feedback message")

        is_positive = feedback_value == "good-feedback"
        emoji = "👍" if is_positive else "👎"
        text = f"{emoji} Feedback from <@{user_id}>"
        if link:
            text += f" on {link}"
        text += f":\n{comment}" if comment else "\n(no comment)"

        notify_admin(
            text,
            dedupe_key=f"feedback:{channel_id}:{message_ts}:{user_id}:{feedback_value}",
            min_interval_seconds=5,
        )

        client.chat_postEphemeral(
            channel=channel_id,
            user=user_id,
            thread_ts=message_ts,
            text="Thanks for the feedback!" if is_positive else "Thanks, noted — sorry that missed the mark.",
        )
    except Exception as e:
        logger.exception("Failed to handle feedback submission: %s", e)
