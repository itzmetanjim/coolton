import pytest

from web import conversation_log as log


@pytest.fixture(autouse=True)
def tmp_store(monkeypatch, tmp_path):
    monkeypatch.setattr(log, "STORE_DIR", str(tmp_path / "web_conversations"))
    log._locks.clear()
    log._last_seq.clear()
    with log._subscribers_guard:
        log._subscribers.clear()
    return tmp_path


def test_create_conversation_is_owned_and_listed():
    cid = log.create_conversation("U1", title="hello")
    assert log.is_owner(cid, "U1")
    assert not log.is_owner(cid, "U2")
    rows = log.list_conversations("U1")
    assert len(rows) == 1
    assert rows[0]["id"] == cid
    assert rows[0]["title"] == "hello"


def test_list_conversations_only_returns_the_owners_own():
    log.create_conversation("U1", title="a")
    log.create_conversation("U2", title="b")
    rows = log.list_conversations("U1")
    assert len(rows) == 1
    assert rows[0]["title"] == "a"


def test_list_conversations_sorted_newest_first():
    cid1 = log.create_conversation("U1", title="first")
    cid2 = log.create_conversation("U1", title="second")
    log.set_title(cid1, "first")  # bumps updated_at on cid1
    rows = log.list_conversations("U1")
    assert [r["id"] for r in rows] == [cid1, cid2]


def test_append_event_assigns_monotonic_seq():
    cid = log.create_conversation("U1")
    e1 = log.append_event(cid, {"type": "user_message", "text": "hi"})
    e2 = log.append_event(cid, {"type": "turn_start"})
    assert e1["seq"] == 1
    assert e2["seq"] == 2
    assert "ts" in e1


def test_append_event_seq_survives_across_module_state_reload():
    """A fresh process re-derives the next seq from what's already on disk,
    not from in-memory state — simulate that by clearing the in-memory cache."""
    cid = log.create_conversation("U1")
    log.append_event(cid, {"type": "user_message", "text": "hi"})
    log._last_seq.clear()
    e2 = log.append_event(cid, {"type": "turn_start"})
    assert e2["seq"] == 2


def test_read_events_after_filters_correctly():
    cid = log.create_conversation("U1")
    log.append_event(cid, {"type": "a"})
    log.append_event(cid, {"type": "b"})
    log.append_event(cid, {"type": "c"})
    events = log.read_events(cid, after=1)
    assert [e["type"] for e in events] == ["b", "c"]


def test_read_events_unknown_conversation_returns_empty():
    assert log.read_events("does-not-exist") == []


def test_subscribe_receives_future_events_not_past_ones():
    cid = log.create_conversation("U1")
    log.append_event(cid, {"type": "before"})

    received = []
    unsubscribe = log.subscribe(cid, received.append)
    log.append_event(cid, {"type": "after"})
    unsubscribe()
    log.append_event(cid, {"type": "ignored"})

    assert [e["type"] for e in received] == ["after"]


def test_unsubscribe_stops_delivery():
    cid = log.create_conversation("U1")
    received = []
    unsubscribe = log.subscribe(cid, received.append)
    unsubscribe()
    log.append_event(cid, {"type": "x"})
    assert received == []


def test_repair_orphaned_turns_closes_a_turn_left_open_by_a_restart():
    cid = log.create_conversation("U1")
    log.append_event(cid, {"type": "turn_start"})
    repaired = log.repair_orphaned_turns()
    assert repaired == 1
    events = log.read_events(cid)
    assert events[-1]["type"] == "turn_end"
    assert events[-1]["state"] == "error"


def test_repair_orphaned_turns_leaves_a_completed_turn_alone():
    cid = log.create_conversation("U1")
    log.append_event(cid, {"type": "turn_start"})
    log.append_event(cid, {"type": "turn_end", "state": "complete"})
    assert log.repair_orphaned_turns() == 0


def test_repair_orphaned_turns_handles_a_second_turn_after_a_completed_one():
    """Only the LATEST turn_start matters — an earlier, already-closed turn
    must not cause a false-positive repair."""
    cid = log.create_conversation("U1")
    log.append_event(cid, {"type": "turn_start"})
    log.append_event(cid, {"type": "turn_end", "state": "complete"})
    log.append_event(cid, {"type": "turn_start"})
    assert log.repair_orphaned_turns() == 1


def test_delete_conversation_removes_the_index_entry_and_the_log():
    cid = log.create_conversation("U1", title="doomed")
    log.append_event(cid, {"type": "user_message", "text": "hi"})

    assert log.delete_conversation(cid) is True
    assert log.get_conversation_meta(cid) is None
    assert log.list_conversations("U1") == []
    assert log.read_events(cid) == []
    # A deleted id must not resurrect its old seq counter if reused.
    assert log._last_seq.get(cid) is None


def test_delete_conversation_reports_when_there_was_nothing_to_delete():
    assert log.delete_conversation("does-not-exist") is False


def test_delete_conversation_leaves_other_conversations_alone():
    keep = log.create_conversation("U1", title="keep")
    drop = log.create_conversation("U1", title="drop")
    log.delete_conversation(drop)
    assert [r["id"] for r in log.list_conversations("U1")] == [keep]


def test_delete_conversation_kills_its_sandbox(monkeypatch):
    """Otherwise a deleted conversation's sandbox (and its github_proxy
    token) keeps running indefinitely — nothing else ever tears it down."""
    from web.runner import WEB_CHANNEL_ID

    calls = []
    monkeypatch.setattr("agent.sandbox_helpers.kill_thread_sandbox", lambda ch, tid: calls.append((ch, tid)))

    cid = log.create_conversation("U1", title="doomed")
    log.delete_conversation(cid)

    assert calls == [(WEB_CHANNEL_ID, cid)]


def test_delete_conversation_still_succeeds_if_sandbox_teardown_fails(monkeypatch):
    def boom(ch, tid):
        raise RuntimeError("e2b unreachable")

    monkeypatch.setattr("agent.sandbox_helpers.kill_thread_sandbox", boom)

    cid = log.create_conversation("U1", title="doomed")
    assert log.delete_conversation(cid) is True
    assert log.get_conversation_meta(cid) is None


def test_title_from_message_uses_the_first_non_empty_line():
    assert log.title_from_message("\n\n  deploy the thing  \nand then some") == "deploy the thing"


def test_title_from_message_collapses_whitespace():
    assert log.title_from_message("deploy    the\tthing") == "deploy the thing"


def test_title_from_message_cuts_long_text_on_a_word_boundary():
    text = "check whether the caddy config on the box is actually proxying port 34343 for me"
    title = log.title_from_message(text, limit=52)
    assert title.endswith("…")
    assert len(title) <= 53
    # Cut between words, never mid-word.
    assert text.startswith(title[:-1])
    assert title[-2] != " "
    assert text[len(title) - 1] == " "


def test_title_from_message_is_empty_for_an_empty_message():
    assert log.title_from_message("   \n  ") == ""
