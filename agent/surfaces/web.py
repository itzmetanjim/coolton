"""Web implementation of agent.surface.Surface.

Every method appends a structured JSON event into the conversation's log
(web.conversation_log) instead of talking to Slack. Args/results are stored as
real JSON, not the {key=value} truncated string agent.plan_block builds for
Slack's plain-text card — see agent.surfaces.web_hooks for the "thinking block"
equivalent this drives on the frontend.
"""

from __future__ import annotations

from typing import Any


class WebSurface:
    name = "web"

    def __init__(self, conversation_id: str, owner_id: str):
        self.conversation_id = conversation_id
        self.owner_id = owner_id
        # The event this turn's reactions target — set by web.runner right after
        # it appends the user_message event that started this turn.
        self.target_seq: int | None = None

    def set_target_message(self, seq: int) -> None:
        self.target_seq = seq

    def _append(self, event: dict) -> dict:
        from web import conversation_log as log
        return log.append_event(self.conversation_id, event)

    def post_text(self, text: str) -> None:
        self._append({"type": "agent_message", "variant": "status", "text": text})

    def post_final(self, text: str) -> None:
        self._append({"type": "agent_message", "variant": "final", "text": text})

    def post_error(self, text: str) -> None:
        self._append({"type": "turn_end", "state": "error", "reason": text})

    def post_image(self, image_url: str, alt_text: str) -> str | None:
        self._append({"type": "agent_message", "variant": "image", "url": image_url, "alt_text": alt_text})
        return None

    def post_file_link(self, url: str, filename: str, title: str = "", comment: str = "") -> str:
        self._append({
            "type": "agent_message", "variant": "file",
            "url": url, "filename": filename, "title": title, "comment": comment,
        })
        return f"Uploaded {filename} and posted the link in this conversation."

    def post_embed(self, url: str, title: str, text: str, thumbnail_url: str | None = None) -> str:
        self._append({
            "type": "agent_message", "variant": "embed",
            "url": url, "title": title, "text": text, "thumbnail_url": thumbnail_url,
        })
        return f"Success: embed posted to conversation {self.conversation_id}"

    def react(self, emoji_name: str) -> str:
        self._append({"type": "reaction", "op": "add", "emoji": emoji_name, "target_seq": self.target_seq})
        return f"Reacted with :{emoji_name}:"

    def remove_reaction(self, emoji_name: str, timestamp: str = "") -> str:
        self._append({"type": "reaction", "op": "remove", "emoji": emoji_name, "target_seq": self.target_seq})
        return f"Removed :{emoji_name}: reaction."

    def set_activity(self, text: str) -> None:
        self._append({"type": "turn_status", "text": text})

    def download_attachments(self, sandbox: Any, limit: int = 20) -> str:
        from web.conversations import download_conversation_attachments
        return download_conversation_attachments(self.conversation_id, sandbox, limit)

    def summarize(self) -> str:
        from web.conversations import summarize_conversation
        return summarize_conversation(self.conversation_id)

    def set_engaged(self, engaged: bool) -> str:
        return "Not applicable on the web UI — every message here already gets a turn."

    def build_hooks(self, deps: Any) -> Any | None:
        from agent.surfaces.web_hooks import build_web_hooks
        return build_web_hooks(self.conversation_id)

    def set_model(self, deps: Any, model_used: str) -> None:
        self._append({"type": "step", "kind": "model", "status": "complete", "text": model_used})

    def finish_turn(self, deps: Any) -> None:
        if getattr(deps, "should_skip", False):
            reason = getattr(deps, "halt_reason", "") or "skipped"
            state = "stopped" if "!stop" in reason else "skipped"
            self._append({"type": "turn_end", "state": state, "reason": reason})
        else:
            self._append({"type": "turn_end", "state": "complete"})
