"""Slack implementation of agent.surface.Surface.

Every method here is a direct lift of what agent.agent / agent.plan_block /
agent.thread_status / agent.tools.emoji_reaction already did when talking
about "the current thread" — this file doesn't change Slack's behavior, it
just gives it a name other than "whatever agent.py happened to inline".
"""

from __future__ import annotations

import logging
from typing import Any

from agent.redact import redact as _redact

logger = logging.getLogger(__name__)


class SlackSurface:
    name = "slack"

    def __init__(self, client, channel_id: str, thread_ts: str, message_ts: str, user_token: str | None = None):
        self.client = client
        self.channel_id = channel_id
        self.thread_ts = thread_ts
        self.message_ts = message_ts
        self.user_token = user_token

    def post_text(self, text: str) -> None:
        try:
            self.client.chat_postMessage(
                channel=self.channel_id,
                thread_ts=self.thread_ts,
                markdown_text=_redact(text, context="send_message"),
            )
        except Exception as e:
            logger.warning(f"Failed to post message: {e}")

    def post_final(self, text: str) -> None:
        # Streaming (say_stream) is handled by listeners.events.turn directly, since it
        # needs Bolt's SayStream object — this is the non-streaming fallback path only.
        self.post_text(text)

    def post_error(self, text: str) -> None:
        try:
            self.client.chat_postMessage(
                channel=self.channel_id,
                thread_ts=self.thread_ts,
                text=f":warning: Something went wrong! ({_redact(text)})",
            )
        except Exception as e:
            logger.warning(f"Failed to post error: {e}")

    def post_image(self, image_url: str, alt_text: str) -> str | None:
        from agent.agent import _post_image_to_channel
        return _post_image_to_channel(self.channel_id, self.thread_ts, image_url, alt_text)

    def post_file_link(self, url: str, filename: str, title: str = "", comment: str = "") -> str:
        label = title or filename
        message = f"{comment}\n\n📄 *{label}*: {url}" if comment else f"📄 *{label}*: {url}"
        post_kwargs = {"channel": self.channel_id, "text": message}
        if self.thread_ts:
            post_kwargs["thread_ts"] = self.thread_ts
        try:
            resp = self.client.chat_postMessage(**post_kwargs)
        except Exception as e:
            return f"File hosted at {url}, but posting the link failed: {e}"
        if not resp.get("ok"):
            return f"File hosted at {url}, but posting the link failed: {resp}"
        return f"Uploaded {filename} and posted the link in the thread."

    def post_embed(self, url: str, title: str, text: str, thumbnail_url: str | None = None) -> str:
        from agent.agent import send_web_embed
        kwargs = {"channel_id": self.channel_id, "text": text, "url": url, "title": title, "thread_ts": self.thread_ts}
        if thumbnail_url:
            kwargs["thumbnail_url"] = thumbnail_url
        return send_web_embed(**kwargs)

    def react(self, emoji_name: str) -> str:
        from slack_sdk.errors import SlackApiError

        try:
            self.client.reactions_add(channel=self.channel_id, timestamp=self.message_ts, name=emoji_name)
            return f"Reacted with :{emoji_name}:"
        except SlackApiError as e:
            return f"Could not add reaction: {e.response['error']}"
        except Exception as e:
            return f"Could not add reaction: {e}"

    def remove_reaction(self, emoji_name: str, timestamp: str = "") -> str:
        from agent.tools.slack_info import remove_emoji_reaction
        return remove_emoji_reaction(self.channel_id, timestamp or self.message_ts, emoji_name)

    def set_activity(self, text: str) -> None:
        import agent.thread_status as thread_status
        thread_status.set_status(self.channel_id, self.thread_ts, text)

    def download_attachments(self, sandbox: Any, limit: int = 20) -> str:
        from agent.agent import download_slack_attachments
        return download_slack_attachments(self.channel_id, self.thread_ts, sandbox, self.user_token, limit)

    def summarize(self) -> str:
        from agent.tools.summarize_thread import summarize_thread
        return summarize_thread(self.channel_id, self.thread_ts, self.user_token)

    def set_engaged(self, engaged: bool) -> str:
        from agent.leave_thread_store import join_thread, leave_thread
        return join_thread(self.channel_id, self.thread_ts) if engaged else leave_thread(self.channel_id, self.thread_ts)

    def build_hooks(self, deps: Any) -> Any | None:
        if not deps.plan_ts:
            return None
        from agent.plan_block import build_plan_hooks
        return build_plan_hooks()

    def set_model(self, deps: Any, model_used: str) -> None:
        # No-op: agent.plan_block.set_model_task already renders this straight
        # into the Slack plan block without going through the surface.
        pass

    def finish_turn(self, deps: Any) -> None:
        # No-op: the plan block's own complete_plan_message/set_plan_error calls
        # already convey "this turn is done" for Slack.
        pass
