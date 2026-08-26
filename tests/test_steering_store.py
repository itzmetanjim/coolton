from agent.steering_store import (
    clear_steering_messages,
    peek_steering_messages,
    queue_steering_message,
)


def test_no_messages_queued_by_default():
    assert peek_steering_messages("SS1", "1.1") == []


def test_queued_message_is_visible():
    queue_steering_message("SS2", "1.1", "also check the other thing", "U1")
    messages = peek_steering_messages("SS2", "1.1")
    assert len(messages) == 1
    assert messages[0]["text"] == "also check the other thing"
    assert messages[0]["user_id"] == "U1"


def test_peek_does_not_clear():
    queue_steering_message("SS3", "1.1", "one", "U1")
    peek_steering_messages("SS3", "1.1")
    assert len(peek_steering_messages("SS3", "1.1")) == 1


def test_clear_removes_queued_messages():
    queue_steering_message("SS4", "1.1", "one", "U1")
    clear_steering_messages("SS4", "1.1")
    assert peek_steering_messages("SS4", "1.1") == []


def test_multiple_messages_queue_in_order():
    queue_steering_message("SS5", "1.1", "first", "U1")
    queue_steering_message("SS5", "1.1", "second", "U2")
    messages = peek_steering_messages("SS5", "1.1")
    assert [m["text"] for m in messages] == ["first", "second"]


def test_queue_is_per_thread():
    queue_steering_message("SS6", "1.1", "one", "U1")
    assert peek_steering_messages("SS6", "2.2") == []


def test_clear_on_untracked_thread_is_a_noop():
    clear_steering_messages("SS7", "never-queued")
    assert peek_steering_messages("SS7", "never-queued") == []


def test_stale_messages_are_filtered_out_of_peek():
    queue_steering_message("SS8", "1.1", "old", "U1")
    import agent.steering_store as ss
    ss._queued[("SS8", "1.1")][0]["queued_at"] -= 3600
    assert peek_steering_messages("SS8", "1.1") == []
