from logging import Logger

from slack_sdk import WebClient

from agent.policy_consent import (
    POLICY_CHANNEL_ID,
    clear_pending_for_user,
    pop_pending,
    record_consent,
    revoke_consent,
)
from agent.leave_thread_store import join_thread


def handle_policy_opt_in(ack, body: dict, client: WebClient, logger: Logger):
    ack()
    action = body["actions"][0]
    pending = pop_pending(action.get("value", ""))
    if not pending or pending.get("user_id") != body.get("user", {}).get("id"):
        return

    try:
        join = action.get("action_id") == "policy_opt_in_join"
        if join:
            try:
                client.conversations_invite(channel=POLICY_CHANNEL_ID, users=pending["user_id"])
            except Exception as exc:
                logger.warning("could not invite user to policy channel: %s", exc)
        record_consent(pending["user_id"], joined_policy_channel=join)
        # Stale prompts for the same user (one per pre-consent message) can no
        # longer replay anything useful, so drop them to keep future clicks no-ops.
        clear_pending_for_user(pending["user_id"])

        # A mention on the thread's starter message auto-joins the thread so we
        # respond to every subsequent message (mirroring handle_app_mentioned).
        if pending["thread_ts"] == pending["message_ts"]:
            join_thread(pending["channel_id"], pending["thread_ts"])

        # Don't replay the original message through the agent — that was a
        # surprising side effect of clicking a consent button. Just confirm
        # opt-in succeeded; the user can ask again if they still want an answer.
        client.chat_postEphemeral(
            channel=pending["channel_id"],
            user=pending["user_id"],
            thread_ts=pending["thread_ts"],
            text="Done! Ask anything.",
        )
    except Exception:
        # pending was already popped above, so retrying the button click won't
        # help — without this, a failure here left them with no confirmation
        # and no idea anything went wrong.
        logger.exception("Failed to handle policy opt-in for %s", pending.get("user_id"))
        try:
            client.chat_postMessage(
                channel=pending["channel_id"],
                thread_ts=pending.get("thread_ts"),
                text=":warning: Something went wrong finishing your opt-in — please try again.",
            )
        except Exception:
            pass


def handle_policy_opt_out(ack, body: dict, client: WebClient, logger: Logger):
    ack()
    user_id = body.get("user", {}).get("id")
    try:
        revoke_consent(user_id)
        from listeners.views.app_home_builder import build_app_home_view
        client.views_publish(user_id=user_id, view=build_app_home_view(has_policy_consent=False))
    except Exception:
        logger.exception("Failed to handle policy opt-out for %s", user_id)
