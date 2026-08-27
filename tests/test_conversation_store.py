import json
import threading
from datetime import datetime, timezone

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from thread_context.store import ConversationStore


def _history(*texts: str) -> list:
    parts = [UserPromptPart(content=t, timestamp=datetime.now(timezone.utc)) for t in texts]
    return [ModelRequest(parts=parts)]


def test_set_and_get_roundtrip(tmp_path):
    store = ConversationStore(file_path=str(tmp_path / "conversations.json"))
    history = _history("hello", "world")
    store.set_history("C1", "1.1", history)

    loaded = store.get_history("C1", "1.1")
    assert loaded is not None
    assert len(loaded) == 1
    parts = loaded[0].parts
    assert [p.content for p in parts] == ["hello", "world"]


def test_get_unknown_thread_returns_none(tmp_path):
    store = ConversationStore(file_path=str(tmp_path / "conversations.json"))
    assert store.get_history("C1", "1.1") is None


def test_ttl_expiry_removes_history(tmp_path):
    store = ConversationStore(file_path=str(tmp_path / "conversations.json"), ttl_seconds=-1)
    store.set_history("C1", "1.1", _history("hi"))
    assert store.get_history("C1", "1.1") is None


def test_max_conversations_evicts_oldest(tmp_path):
    store = ConversationStore(file_path=str(tmp_path / "conversations.json"), max_conversations=2)
    store.set_history("C1", "1.1", _history("a"))
    store.set_history("C2", "2.2", _history("b"))
    store.set_history("C3", "3.3", _history("c"))

    assert store.get_history("C1", "1.1") is None
    assert store.get_history("C2", "2.2") is not None
    assert store.get_history("C3", "3.3") is not None


def test_persists_and_reloads_from_disk(tmp_path):
    path = str(tmp_path / "conversations.json")
    store = ConversationStore(file_path=path)
    store.set_history("C1", "1.1", _history("persisted"))

    reloaded = ConversationStore(file_path=path)
    loaded = reloaded.get_history("C1", "1.1")
    assert loaded is not None
    assert loaded[0].parts[0].content == "persisted"


def test_malformed_entries_skipped_on_load(tmp_path):
    path = str(tmp_path / "conversations.json")
    # one valid-looking entry, one missing timestamp, one with bad messages
    data = {
        "C1:1.1": {"timestamp": datetime.now().timestamp(), "messages": []},
        "C2:2.2": {"nope": True},
        "C3:3.3": {"timestamp": datetime.now().timestamp(), "messages": "not-a-list"},
    }
    tmp_path.joinpath("conversations.json").write_text(
        __import__("json").dumps(data)
    )

    store = ConversationStore(file_path=path)
    assert store.get_history("C1", "1.1") == []
    assert store.get_history("C2", "2.2") is None
    assert store.get_history("C3", "3.3") is None


def test_corrupt_json_on_disk_no_crash(tmp_path):
    path = str(tmp_path / "conversations.json")
    tmp_path.joinpath("conversations.json").write_text("{total garbage")
    store = ConversationStore(file_path=path)
    assert store.get_history("C1", "1.1") is None
    # still writable afterwards
    store.set_history("C1", "1.1", _history("recovered"))
    assert store.get_history("C1", "1.1") is not None


def test_expired_entries_skipped_on_load(tmp_path):
    path = str(tmp_path / "conversations.json")
    old_ts = datetime.now().timestamp() - 100000
    data = {"C1:1.1": {"timestamp": old_ts, "messages": []}}
    tmp_path.joinpath("conversations.json").write_text(__import__("json").dumps(data))

    store = ConversationStore(file_path=path, ttl_seconds=86400)
    assert store.get_history("C1", "1.1") is None


def test_response_messages_roundtrip(tmp_path):
    store = ConversationStore(file_path=str(tmp_path / "conversations.json"))
    response = ModelResponse(
        parts=[TextPart(content="the answer")],
        timestamp=datetime.now(timezone.utc),
    )
    store.set_history("C1", "1.1", [response])
    loaded = store.get_history("C1", "1.1")
    assert loaded is not None
    assert loaded[0].parts[0].content == "the answer"


# ---------------------------------------------------------------------------
# One broken conversation must not stay broken forever — a stored history
# with an unpaired tool call/return gets rejected by every provider on every
# future turn (see agent/history_compaction.py's split-boundary fix for how
# this could happen), so get_history()/_load_from_disk() discard it and let
# the thread start fresh instead of failing identically forever.
# ---------------------------------------------------------------------------


def _paired_tool_round(call_id: str = "call_1") -> list:
    return [
        ModelResponse(parts=[ToolCallPart(tool_name="t", args={}, tool_call_id=call_id)]),
        ModelRequest(parts=[ToolReturnPart(tool_name="t", content="ok", tool_call_id=call_id)]),
    ]


def test_valid_tool_call_return_pair_is_returned_normally(tmp_path):
    store = ConversationStore(file_path=str(tmp_path / "conversations.json"))
    store.set_history("C1", "1.1", _paired_tool_round())
    assert store.get_history("C1", "1.1") is not None


def test_orphaned_tool_return_is_discarded(tmp_path):
    path = str(tmp_path / "conversations.json")
    store = ConversationStore(file_path=path)
    # A return with no matching call anywhere — exactly what the history
    # compaction bug could produce by summarizing the call away.
    broken = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content="ok", tool_call_id="orphan")])]
    store.set_history("C1", "1.1", broken)

    assert store.get_history("C1", "1.1") is None
    # Discarding persists — it's gone on disk too, not just this one read.
    on_disk = json.loads(tmp_path.joinpath("conversations.json").read_text())
    assert "C1:1.1" not in on_disk


def test_dangling_tool_call_is_discarded(tmp_path):
    store = ConversationStore(file_path=str(tmp_path / "conversations.json"))
    # A call with no return anywhere — providers reject this just as hard.
    broken = [ModelResponse(parts=[ToolCallPart(tool_name="t", args={}, tool_call_id="never_returned")])]
    store.set_history("C1", "1.1", broken)

    assert store.get_history("C1", "1.1") is None


def test_thread_recovers_after_broken_history_is_discarded(tmp_path):
    """The whole point: one broken conversation degrades to "lost its memory",
    not "permanently wedged" — the thread must be usable again right away."""
    store = ConversationStore(file_path=str(tmp_path / "conversations.json"))
    broken = [ModelRequest(parts=[ToolReturnPart(tool_name="t", content="ok", tool_call_id="orphan")])]
    store.set_history("C1", "1.1", broken)
    assert store.get_history("C1", "1.1") is None

    store.set_history("C1", "1.1", _history("fresh start"))
    assert store.get_history("C1", "1.1") is not None


def test_invalid_history_skipped_on_load_from_disk(tmp_path):
    path = str(tmp_path / "conversations.json")
    orphan_return = {
        "parts": [{
            "part_kind": "tool-return",
            "tool_name": "t",
            "content": "ok",
            "tool_call_id": "orphan",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
        "kind": "request",
    }
    data = {"C1:1.1": {"timestamp": datetime.now().timestamp(), "messages": [orphan_return]}}
    tmp_path.joinpath("conversations.json").write_text(json.dumps(data))

    store = ConversationStore(file_path=path)
    assert store.get_history("C1", "1.1") is None


def test_concurrent_set_history_across_channels_all_persist(tmp_path):
    """set_history serializes/writes to disk outside the store's main lock so
    one channel's write can't block another's — concurrent writers for
    DIFFERENT conversations must all still land in the final on-disk file."""
    path = str(tmp_path / "conversations.json")
    store = ConversationStore(file_path=path)
    n = 20

    def write(i):
        store.set_history(f"C{i}", f"{i}.{i}", _history(f"msg-{i}"))

    threads = [threading.Thread(target=write, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for i in range(n):
        loaded = store.get_history(f"C{i}", f"{i}.{i}")
        assert loaded is not None, f"conversation {i} missing from in-memory store"
        assert loaded[0].parts[0].content == f"msg-{i}"

    on_disk = json.loads(tmp_path.joinpath("conversations.json").read_text())
    assert len(on_disk) == n, "on-disk file must contain every conversation, not just the last write"


def test_stale_write_does_not_clobber_newer_write_on_disk(tmp_path):
    """_write_snapshot must skip an out-of-order (stale) write rather than
    overwrite a newer snapshot that already landed — this is what makes it
    safe to serialize/write outside the store's main lock."""
    path = str(tmp_path / "conversations.json")
    store = ConversationStore(file_path=path)

    newer = {("C1", "1.1"): {"messages": _history("newer"), "timestamp": 2.0}}
    older = {("C1", "1.1"): {"messages": _history("older"), "timestamp": 1.0}}

    # Write the "newer" snapshot (seq=2) first, then a "stale" one (seq=1)
    # arriving late — as could happen if two threads' disk writes complete
    # out of order.
    store._write_snapshot(newer, seq=2)
    store._write_snapshot(older, seq=1)

    on_disk = json.loads(tmp_path.joinpath("conversations.json").read_text())
    entry = on_disk["C1:1.1"]
    texts = [part["content"] for msg in entry["messages"] for part in msg["parts"]]
    assert texts == ["newer"], "a stale write must not clobber a newer one already on disk"


def test_conversation_trace_contains_thread_metadata_and_all_trace_parts(tmp_path):
    from pydantic_ai.messages import ThinkingPart, ToolCallPart, ToolReturnPart
    from thread_context.training_log import ConversationTraceStore

    now = datetime.now(timezone.utc)
    messages = [
        ModelRequest(parts=[UserPromptPart(content="fix it", timestamp=now)]),
        ModelResponse(parts=[
            ThinkingPart(content="inspect the failing test"),
            ToolCallPart(tool_name="read_file", args={"path": "tests/test_app.py"}, tool_call_id="c1"),
        ]),
        ModelRequest(parts=[
            ToolReturnPart(tool_name="read_file", content="assert False", tool_call_id="c1"),
        ]),
        ModelResponse(parts=[TextPart(content="fixed it")]),
    ]
    path = ConversationTraceStore(str(tmp_path / "logs")).write("C/one", "1.2", messages)
    import json
    document = json.loads(path.read_text())
    assert document["channel_id"] == "C/one"
    assert document["thread_ts"] == "1.2"
    parts = [part for message in document["messages"] for part in message["parts"]]
    assert {part["type"] for part in parts} == {"user", "thinking", "tool_call", "tool_result", "output"}
    assert next(part for part in parts if part["type"] == "thinking")["content"] == "inspect the failing test"
    assert next(part for part in parts if part["type"] == "tool_call")["tool_name"] == "read_file"
