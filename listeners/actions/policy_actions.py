from logging import Logger

from slack_sdk import WebClient

from agent.policy_consent import POLICY_CHANNEL_ID, pop_pending, record_consent, revoke_consent
from listeners.events.turn import run_agent_turn


class _ActionStream:
    def __init__(self, client: WebClient, channel: str, thread_ts: str):
        self.client, self.channel, self.thread_ts = client, channel, thread_ts
        self.text = ""

    def __call__(self):
        return self

    def append(self, *, markdown_text: str):
        self.text += markdown_text

    def stop(self, *, blocks=None):
        payload = {"channel": self.channel, "thread_ts": self.thread_ts, "text": self.text}
        if blocks:
            payload["blocks"] = blocks
        self.client.chat_postMessage(**payload)


def handle_policy_opt_in(ack, body: dict, client: WebClient, logger: Logger):
    ack()
    action = body["actions"][0]
    pending = pop_pending(action.get("value", ""))
    if not pending or pending.get("user_id") != body.get("user", {}).get("id"):
        return

    join = action.get("action_id") == "policy_opt_in_join"
    if join:
        try:
            client.conversations_invite(channel=POLICY_CHANNEL_ID, users=pending["user_id"])
        except Exception as exc:
            logger.warning("could not invite user to policy channel: %s", exc)
    record_consent(pending["user_id"], joined_policy_channel=join)

    from agent.tools.vision import download_attached_images
    images = download_attached_images(client, pending.get("files"))
    run_agent_turn(
        client=client,
        say_stream=_ActionStream(client, pending["channel_id"], pending["thread_ts"]),
        say=lambda **kwargs: client.chat_postMessage(**kwargs),
        logger=logger,
        channel_id=pending["channel_id"], thread_ts=pending["thread_ts"],
        message_ts=pending["message_ts"], user_id=pending["user_id"],
        user_token=pending.get("user_token"), text=pending["text"],
        history=_history_for_pending(client, pending), images=images,
    )


def handle_policy_opt_out(ack, body: dict, client: WebClient, logger: Logger):
    ack()
    user_id = body.get("user", {}).get("id")
    revoke_consent(user_id)
    from listeners.views.app_home_builder import build_app_home_view
    client.views_publish(user_id=user_id, view=build_app_home_view(has_policy_consent=False))


def _history_for_pending(client: WebClient, pending: dict):
    from thread_context import conversation_store
    history = conversation_store.get_history(pending["channel_id"], pending["thread_ts"])
    if history is not None:
        return history
    if pending["thread_ts"] != pending["message_ts"]:
        from thread_context.thread_history import build_thread_context
        return build_thread_context(client, pending["channel_id"], pending["thread_ts"], exclude_ts=pending["message_ts"])
    return None
