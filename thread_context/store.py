"""Each conversation's message history (pydantic-ai ModelMessages), kept as one
JSON file per conversation under conversations/, so a turn rewrites just its own
conversation, not every one coolton remembers. A conversation nobody has written
to for `ttl_seconds` is forgotten."""

import logging
import os
import threading
import time
from urllib.parse import quote

from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessage

from agent.json_file import load_json, save_json
from agent.keyed_locks import KeyedLocks

logger = logging.getLogger(__name__)

# Initialize the TypeAdapter to handle rich Pydantic AI message schemas
ModelMessagesTypeAdapter = TypeAdapter(list[ModelMessage])


def _is_valid_history(messages: list[ModelMessage]) -> bool:
    """A stored history is only safe to resend if every tool call has exactly
    one matching return and vice versa — every provider rejects a call with
    no return (or a return with no call) as a 400, so one thread that somehow
    ends up with an unpaired one would otherwise fail the exact same way on
    every single future turn, burning a full provider-fallback retry each
    time with no way to recover on its own. However the mismatch got there
    (a bug in compaction, a crash mid-write, anything), catching it here
    means one broken conversation degrades to "lost its memory" instead of
    "permanently wedged."
    """
    call_ids: set[str] = set()
    return_ids: set[str] = set()
    for msg in messages:
        for part in getattr(msg, "parts", []):
            tool_call_id = getattr(part, "tool_call_id", None)
            if not tool_call_id:
                continue
            part_kind = getattr(part, "part_kind", None)
            if part_kind == "tool-call":
                call_ids.add(tool_call_id)
            elif part_kind == "tool-return":
                return_ids.add(tool_call_id)
            elif part_kind == "retry-prompt" and getattr(part, "tool_name", None):
                # A tool call with bad arguments is answered by a retry prompt, which the
                # model APIs take as that call's result. (One with no tool_name answers no
                # call: it's about the output, and its tool_call_id is made up.) Not
                # counting these threw away the memory of every thread with one.
                return_ids.add(tool_call_id)
    return call_ids == return_ids


def _latest_ts(a: str | None, b: str | None) -> str | None:
    """The later of two Slack timestamps (either may be None or malformed)."""
    def as_float(ts):
        try:
            return float(ts)
        except (TypeError, ValueError):
            return None
    fa, fb = as_float(a), as_float(b)
    if fa is None:
        return b if fb is not None else None
    if fb is None:
        return a
    return a if fa >= fb else b


class ConversationStore:
    """Thread-safe, file-persisted conversation history store."""

    def __init__(
        self,
        directory: str = "conversations",
        ttl_seconds: int = 90 * 24 * 60 * 60,
        legacy_file: str | None = "conversations.json",
    ):
        self._directory = directory
        self._ttl_seconds = ttl_seconds
        self._legacy_file = legacy_file
        self._locks = KeyedLocks()
        self._prepared = False
        self._prepare_lock = threading.Lock()

    def _prepare(self) -> None:
        """Migrate the old single file and forget expired conversations, once, on first
        use. Not on construction: thread_context makes the store at import, and merely
        importing coolton (a test run in the live checkout, say) must not touch the
        live conversations."""
        if self._prepared:
            return
        with self._prepare_lock:
            if not self._prepared:
                if self._legacy_file:
                    self._migrate(self._legacy_file)
                self._forget_expired()
                self._prepared = True

    def _path(self, channel_id: str, thread_ts: str) -> str:
        # Quoted, so whatever the ids hold it's one file in the directory (":" becomes %3A).
        return os.path.join(self._directory, quote(f"{channel_id}:{thread_ts}", safe="") + ".json")

    def _load(self, channel_id: str, thread_ts: str) -> dict | None:
        """The conversation's stored entry with its messages deserialized, or None. An
        expired history is deleted, and so is one that can't be resent: unreadable, or
        with a tool call and return that don't pair up (see _is_valid_history)."""
        path = self._path(channel_id, thread_ts)
        entry = load_json(path, default=lambda: None)
        if entry is None:
            return None
        written_at = entry.get("timestamp")
        if not isinstance(written_at, (int, float)) or time.time() - written_at > self._ttl_seconds:
            _remove(path)
            return None
        try:
            messages = ModelMessagesTypeAdapter.validate_python(entry.get("messages"))
        except ValueError as e:
            logger.error("Discarding unreadable stored history for %s:%s: %s", channel_id, thread_ts, e)
            _remove(path)
            return None
        if not _is_valid_history(messages):
            logger.error(
                "Discarding structurally invalid stored history for %s:%s "
                "(unpaired tool call/return) — this thread will start fresh "
                "instead of failing every turn identically.",
                channel_id, thread_ts,
            )
            _remove(path)
            return None
        return {"messages": messages, "last_seen_ts": entry.get("last_seen_ts")}

    def get_history(self, channel_id: str, thread_ts: str) -> list[ModelMessage] | None:
        """Retrieve conversation history for a thread.

        Returns None if no history exists or if the history has expired.
        """
        self._prepare()
        with self._locks.hold((channel_id, thread_ts)):
            entry = self._load(channel_id, thread_ts)
        return entry["messages"] if entry else None

    def set_history(
        self, channel_id: str, thread_ts: str, messages: list[ModelMessage],
        last_seen_ts: str | None = None,
    ) -> None:
        """Store conversation history for a thread and persist to disk.

        `last_seen_ts` is the Slack ts of the latest thread message this history
        covers (see get_last_seen_ts). It only ever moves forward.
        """
        self._prepare()
        path = self._path(channel_id, thread_ts)
        try:
            with self._locks.hold((channel_id, thread_ts)):
                previous = load_json(path).get("last_seen_ts")
                save_json(path, {
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "messages": ModelMessagesTypeAdapter.dump_python(messages, mode="json"),
                    "timestamp": time.time(),
                    "last_seen_ts": _latest_ts(previous, last_seen_ts),
                }, private=True, indent=None)
        except Exception as e:
            logger.error(f"Failed to save conversation {channel_id}:{thread_ts} to disk: {e}")

    def get_last_seen_ts(self, channel_id: str, thread_ts: str) -> str | None:
        """Slack ts of the latest thread message coolton's stored history for
        this thread covers — anything posted after it (while coolton wasn't
        mentioned) is something it hasn't seen. None if unknown."""
        self._prepare()
        with self._locks.hold((channel_id, thread_ts)):
            entry = self._load(channel_id, thread_ts)
        return entry["last_seen_ts"] if entry else None

    def _forget_expired(self) -> None:
        """Delete every conversation nobody has written to within the TTL (a file's
        mtime is its last write)."""
        cutoff = time.time() - self._ttl_seconds
        try:
            files = list(os.scandir(self._directory))
        except FileNotFoundError:
            return
        for f in files:
            try:
                if f.name.endswith(".json") and f.stat().st_mtime < cutoff:
                    os.remove(f.path)
            except OSError:
                logger.exception("Couldn't forget expired conversation %s", f.path)

    def _migrate(self, legacy_file: str) -> None:
        """Split the single file every conversation used to share into per-conversation
        files, once; the old file is kept beside them as `<name>.migrated`."""
        if not os.path.exists(legacy_file):
            return
        moved = 0
        for key, entry in load_json(legacy_file).items():
            channel_id, _, thread_ts = key.partition(":")
            path = self._path(channel_id, thread_ts)
            if not isinstance(entry, dict) or os.path.exists(path):
                continue
            save_json(path, {**entry, "channel_id": channel_id, "thread_ts": thread_ts}, private=True, indent=None)
            written_at = entry.get("timestamp")
            if isinstance(written_at, (int, float)):
                os.utime(path, (written_at, written_at))
            moved += 1
        if os.path.exists(legacy_file):
            os.replace(legacy_file, f"{legacy_file}.migrated")
        logger.info("Moved %d conversations from %s into %s/", moved, legacy_file, self._directory)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
