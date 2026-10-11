"""Persist complete, redacted Slack thread traces."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from agent.redact import redact

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
# The newest Slack messages kept beside a trace: a thread rarely has more, but a code
# channel's conversation is the whole channel's history, fetched again every turn.
MAX_SLACK_MESSAGES = 1000


class ConversationTraceStore:
    """Persist one atomically-replaced JSON snapshot per Slack thread."""

    def __init__(self, directory: str = "conversation_logs") -> None:
        self._directory = Path(directory)
        self._lock = threading.Lock()

    def path_for(self, channel_id: str, thread_ts: str) -> Path:
        channel = _SAFE_ID.sub("_", channel_id)
        thread = _SAFE_ID.sub("_", thread_ts)
        return self._directory / f"{channel}__{thread}.json"

    def write(
        self,
        channel_id: str,
        thread_ts: str,
        messages: list[ModelMessage],
        slack_messages: list[dict[str, Any]] | None = None,
    ) -> Path:
        serialized = ModelMessagesTypeAdapter.dump_python(messages, mode="json")
        document = {
            "schema_version": 1,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "updated_at": time.time(),
            "slack_messages": slack_messages or [],
            "messages": [_normalize_message(message) for message in _without_file_bytes(serialized)],
        }
        document = _redact_strings(document)
        destination = self.path_for(channel_id, thread_ts)
        with self._lock:
            self._directory.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=self._directory
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(document, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                os.replace(temporary, destination)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return destination

    def write_from_slack(
        self,
        client: Any,
        channel_id: str,
        thread_ts: str,
        messages: list[ModelMessage],
    ) -> Path:
        """Fetch the Slack thread (up to MAX_SLACK_MESSAGES of it) and persist it beside
        the model trace. A code channel's conversation (thread_ts "",
        agent.code_channel_store) is the channel's own top-level messages, newest first."""
        fetch = (client.conversations_replies if thread_ts else client.conversations_history)
        params = {"channel": channel_id, "limit": 200, **({"ts": thread_ts} if thread_ts else {})}
        response = fetch(**params)
        slack_messages = response.get("messages", [])
        while (len(slack_messages) < MAX_SLACK_MESSAGES and response.get("has_more")
               and response.get("response_metadata", {}).get("next_cursor")):
            response = fetch(**params, cursor=response["response_metadata"]["next_cursor"])
            slack_messages.extend(response.get("messages", []))
        slack_messages = slack_messages[:MAX_SLACK_MESSAGES]
        if not thread_ts:
            slack_messages.reverse()  # history is newest first; replies are oldest first
        return self.write(channel_id, thread_ts, messages, slack_messages)


def _without_file_bytes(value: Any) -> Any:
    """`value` with every attached file's bytes (an image the model saw, a screenshot)
    replaced by a note of its type and size: a trace keeps what was said and done, and a
    few screenshots would otherwise make up most of it."""
    if isinstance(value, list):
        return [_without_file_bytes(item) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("kind") == "binary" and isinstance(value.get("data"), str):
        size = len(value["data"]) * 3 // 4 - value["data"][-2:].count("=")  # base64-decoded length
        return {**value, "data": f"<{value.get('media_type', 'file')}, {size} bytes, not kept>"}
    if isinstance(value.get("url"), str) and value["url"].startswith("data:"):
        return {**value, "url": "<data: URL, not kept>"}
    return {key: _without_file_bytes(item) for key, item in value.items()}


def _redact_strings(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value, context="conversation trace")
    if isinstance(value, dict):
        return {key: _redact_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_strings(item) for item in value]
    return value


def _normalize_message(message: dict[str, Any]) -> dict[str, Any]:
    """Flatten Pydantic AI's provider schema into role/type/content records."""
    kind = message.get("kind")
    role = "user" if kind == "request" else "assistant" if kind == "response" else kind
    parts = []
    for part in message.get("parts") or []:
        part_kind = part.get("part_kind", "unknown")
        if part_kind == "user-prompt":
            part_type, part_role = "user", "user"
        elif part_kind == "thinking":
            part_type, part_role = "thinking", "assistant"
        elif part_kind in {"tool-call", "tool-search-call"}:
            part_type, part_role = "tool_call", "assistant"
        elif part_kind in {"tool-return", "tool-search-return"}:
            part_type, part_role = "tool_result", "tool"
        elif part_kind == "text":
            part_type, part_role = "output", "assistant"
        else:
            part_type, part_role = part_kind, role
        clean = dict(part)
        clean.pop("part_kind", None)
        parts.append({"role": part_role, "type": part_type, **clean})
    return {"role": role, "kind": kind, "parts": parts}
