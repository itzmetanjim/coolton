import os
import json
import tempfile
import time
import logging
import threading
from pydantic import TypeAdapter
from pydantic_ai.messages import ModelMessage

logger = logging.getLogger(__name__)

# Initialize the TypeAdapter to handle rich Pydantic AI message schemas
ModelMessagesTypeAdapter = TypeAdapter(list[ModelMessage])


class ConversationStore:
    """Thread-safe, file-persisted conversation history store.

    Stores Pydantic AI message histories both in memory (for maximum speed) 
    and in a local JSON file (so conversations survive server restarts).
    """

    def __init__(
        self, 
        file_path: str = "conversations.json", 
        ttl_seconds: int = 86400, 
        max_conversations: int = 1000
    ):
        self._file_path = file_path
        self._ttl_seconds = ttl_seconds
        self._max_conversations = max_conversations
        self._lock = threading.Lock()

        # Snapshots are serialized and written to disk OUTSIDE self._lock (see
        # set_history), so concurrent writers can complete out of order. This
        # sequence number + lock (much smaller critical section than the full
        # serialize+write) ensures a stale/older snapshot can never clobber a
        # newer one that already landed on disk.
        self._write_order_lock = threading.Lock()
        self._write_seq = 0
        self._written_seq = 0

        # In-memory store: keyed by (channel_id, thread_ts)
        self._store: dict[tuple[str, str], dict] = {}
        
        # Load existing histories from disk on startup
        self._load_from_disk()

    def get_history(self, channel_id: str, thread_ts: str) -> list[ModelMessage] | None:
        """Retrieve conversation history for a thread.

        Returns None if no history exists or if the history has expired.
        """
        key = (channel_id, thread_ts)
        snapshot = None
        seq = None
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None

            # Check if expired
            if time.time() - entry["timestamp"] > self._ttl_seconds:
                del self._store[key]
                snapshot = dict(self._store)
                self._write_seq += 1
                seq = self._write_seq
            else:
                return entry["messages"]

        # Persisting the expiry is done outside the lock (see set_history for why).
        self._write_snapshot(snapshot, seq)
        return None

    def set_history(
        self, channel_id: str, thread_ts: str, messages: list[ModelMessage]
    ) -> None:
        """Store conversation history for a thread and persist to disk."""
        key = (channel_id, thread_ts)
        with self._lock:
            self._store[key] = {
                "messages": messages,
                "timestamp": time.time(),
            }
            self._cleanup()
            snapshot = dict(self._store)
            self._write_seq += 1
            seq = self._write_seq
        # Serializing (dump_python + file write) is O(total conversations) —
        # up to max_conversations. Slack Bolt dispatches concurrent events
        # across channels on separate worker threads, so doing this while
        # holding self._lock would block every other channel's get/set_history
        # behind one busy/large conversation's full-store disk write. Take a
        # point-in-time copy under the lock (cheap) and serialize/write outside
        # it instead — see _write_snapshot for the resulting write-ordering
        # tradeoff.
        self._write_snapshot(snapshot, seq)

    def _cleanup(self) -> None:
        """Remove expired entries and enforce max conversation limit.

        NOTE: Must be called inside the lock.
        """
        now = time.time()

        # Remove expired entries
        expired = [
            k
            for k, v in self._store.items()
            if now - v["timestamp"] > self._ttl_seconds
        ]
        for k in expired:
            del self._store[k]

        # Enforce max limit
        if len(self._store) > self._max_conversations:
            sorted_keys = sorted(
                self._store.keys(), key=lambda k: self._store[k]["timestamp"]
            )
            excess = len(self._store) - self._max_conversations
            for k in sorted_keys[:excess]:
                del self._store[k]

    def _load_from_disk(self) -> None:
        """Load and deserialize conversation histories from disk on startup."""
        if not os.path.exists(self._file_path):
            return

        with self._lock:
            try:
                with open(self._file_path, "r") as f:
                    raw_data = json.load(f)
                
                now = time.time()
                loaded_count = 0
                
                for key_str, entry in raw_data.items():
                    try:
                        if not isinstance(entry, dict) or "timestamp" not in entry:
                            raise ValueError("malformed entry (missing timestamp)")
                        # Skip expired histories
                        if now - entry["timestamp"] > self._ttl_seconds:
                            continue
                        
                        # Split the string key "channel_id:thread_ts" back into a tuple
                        channel_id, thread_ts = key_str.split(":", 1)
                        key_tuple = (channel_id, thread_ts)
                        
                        # Deserialize the JSON dictionaries back into rich Pydantic AI objects
                        messages = ModelMessagesTypeAdapter.validate_python(entry["messages"])
                        
                        self._store[key_tuple] = {
                            "messages": messages,
                            "timestamp": entry["timestamp"]
                        }
                        loaded_count += 1
                    except Exception as e:
                        logger.error(f"Failed to deserialize history for {key_str}: {e}")
                
                logger.info(f"Loaded {loaded_count} active conversations from {self._file_path}")
            except Exception as e:
                logger.error(f"Error loading conversations from disk: {e}")

    def _write_snapshot(self, snapshot: dict, seq: int) -> None:
        """Serialize and atomically write a point-in-time copy of the store.

        Deliberately called OUTSIDE self._lock (see set_history/get_history) so
        a full-store JSON dump never blocks other channels' in-memory reads or
        writes. Concurrent calls may therefore finish out of order; `seq`
        (assigned under self._lock at snapshot time) lets a stale write detect
        that a newer one already landed and skip clobbering it, under
        self._write_order_lock — a much smaller critical section than the
        serialize+write this replaces.
        """
        try:
            serialized_data = {}
            for (channel_id, thread_ts), entry in snapshot.items():
                # Convert Pydantic AI's rich ModelMessage objects into plain JSON-serializable types
                serialized_messages = ModelMessagesTypeAdapter.dump_python(
                    entry["messages"],
                    mode="json"
                )

                # Flatten the tuple key to a JSON-compatible string
                key_str = f"{channel_id}:{thread_ts}"
                serialized_data[key_str] = {
                    "messages": serialized_messages,
                    "timestamp": entry["timestamp"]
                }

            # Atomic write (write to a unique temp file first, then replace).
            # A unique name (not a fixed `.tmp` suffix) is required because
            # writers are no longer mutually exclusive here.
            directory = os.path.dirname(os.path.abspath(self._file_path)) or "."
            fd, temp_file = tempfile.mkstemp(prefix=".conversations-", suffix=".tmp", dir=directory)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(serialized_data, f, indent=2)
                with self._write_order_lock:
                    if seq >= self._written_seq:
                        os.replace(temp_file, self._file_path)
                        self._written_seq = seq
                    else:
                        os.unlink(temp_file)
            finally:
                if os.path.exists(temp_file):
                    os.unlink(temp_file)

        except Exception as e:
            logger.error(f"Failed to save conversations to disk: {e}")
