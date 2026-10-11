from agent.steering_store import (
    clear_steering_messages,
    peek_steering_messages,
    queue_steering_message,
    take_next_sender_messages,
    take_steering_messages,
)


def test_queued_messages_are_per_thread_and_in_order():
    queue_steering_message("SS5", "1.1", "first", "U1")
    queue_steering_message("SS5", "1.1", "second", "U2")
    assert [m["text"] for m in peek_steering_messages("SS5", "1.1")] == ["first", "second"]
    assert peek_steering_messages("SS5", "2.2") == []
    clear_steering_messages("SS5", "1.1")


def test_a_turn_takes_only_its_own_senders_messages_and_system_notes():
    """Someone else's message folded into a turn would act under the first person's
    identity, and be credited to them."""
    queue_steering_message("SS1", "1.1", "mine", "U1")
    queue_steering_message("SS1", "1.1", "theirs", "U2")
    queue_steering_message("SS1", "1.1", "[SYSTEM: job done]", "")

    taken = take_steering_messages("SS1", "1.1", {"U1"})

    assert [m["text"] for m in taken] == ["mine", "[SYSTEM: job done]"]
    assert [m["text"] for m in peek_steering_messages("SS1", "1.1")] == ["theirs"]
    clear_steering_messages("SS1", "1.1")


def test_leftovers_come_out_one_senders_run_at_a_time():
    for text, user in (("a1", "UA"), ("a2", "UA"), ("b1", "UB"), ("a3", "UA")):
        queue_steering_message("SS2", "1.1", text, user)

    assert [m["text"] for m in take_next_sender_messages("SS2", "1.1")] == ["a1", "a2"]
    assert [m["text"] for m in take_next_sender_messages("SS2", "1.1")] == ["b1"]
    assert [m["text"] for m in take_next_sender_messages("SS2", "1.1")] == ["a3"]
    assert take_next_sender_messages("SS2", "1.1") == []
