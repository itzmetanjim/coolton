"""Unit tests for the persistent stopped-thread store (RFC i rule 2).

After `@<botname> !stop`, every later message in that thread must be ignored
for the lifetime of the process. The store is in-memory by design, matching
coolton's other stores (stop_store, active_runs).
"""

from agent import stopped_threads_store as store


def test_stop_key_prefers_thread_ts():
    assert store.stop_key("C1", "1.1") == "1.1"


def test_stop_key_falls_back_to_channel_for_dm():
    """A DM message has no thread_ts of its own — the whole DM is the thread.
    Keying on the channel id is what makes a DM-level !stop actually stick;
    keying on the message ts would never match a later message."""
    assert store.stop_key("D1", None) == "D1"


def test_marked_thread_is_stopped():
    store.mark_thread_stopped("C1", "1.1")
    assert store.is_thread_stopped("C1", "1.1") is True


def test_unmarked_thread_is_not_stopped():
    assert store.is_thread_stopped("C1", "1.1") is False


def test_stop_is_permanent_for_the_process_lifetime():
    store.mark_thread_stopped("C1", "1.1")
    store.mark_thread_stopped("C1", "1.1")
    assert store.is_thread_stopped("C1", "1.1") is True


def test_stop_is_per_thread():
    store.mark_thread_stopped("C1", "1.1")
    assert store.is_thread_stopped("C1", "2.2") is False
    assert store.is_thread_stopped("C2", "1.1") is False


def test_dm_stop_covers_whole_dm_regardless_of_message_ts():
    """!stop arrives at ts=1.1; the next DM message is a brand-new ts — it
    must still count as 'in the stopped thread'."""
    store.mark_thread_stopped("D1", None)
    assert store.is_thread_stopped("D1", None) is True


def test_dm_threaded_reply_is_a_separate_thread():
    """A threaded reply inside a stopped DM is its own thread and is not
    caught by the DM-level stop."""
    store.mark_thread_stopped("D1", None)
    assert store.is_thread_stopped("D1", "9.9") is False
