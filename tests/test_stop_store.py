import time

from agent.stop_store import (
    HaltRun,
    is_stop_command,
    request_stop,
    stop_requested_for,
)


def test_halt_run_is_exception():
    assert issubclass(HaltRun, Exception)


def test_no_stop_never_halted():
    assert stop_requested_for("C1", "1.1", run_started_at=0.0) is False


def test_stop_halted_for_runs_that_started_before_request():
    run_started_at = time.time() - 1
    request_stop("C1", "1.1")
    assert stop_requested_for("C1", "1.1", run_started_at) is True


def test_stop_does_not_affect_runs_that_started_after_request():
    request_stop("C1", "1.1")
    run_started_at = time.time() + 1000
    assert stop_requested_for("C1", "1.1", run_started_at) is False


def test_stop_is_per_thread():
    request_stop("C1", "1.1")
    assert stop_requested_for("C2", "2.2", run_started_at=0.0) is False


def test_later_stop_overrides():
    request_stop("C1", "1.1")
    time.sleep(0.01)
    request_stop("C1", "1.1")
    assert stop_requested_for("C1", "1.1", run_started_at=0.0) is True


def test_is_stop_command_bare():
    assert is_stop_command("!stop") is True


def test_is_stop_command_with_surrounding_whitespace():
    assert is_stop_command("  !stop  \n") is True


def test_is_stop_command_with_matching_mention_prefix():
    assert is_stop_command("<@BOT1> !stop", "BOT1") is True


def test_is_stop_command_with_mention_and_extra_whitespace():
    assert is_stop_command("<@BOT1>   !stop", "BOT1") is True


def test_is_stop_command_rejects_embedded_word():
    """A normal prompt merely containing "!stop" must never count."""
    assert is_stop_command("please !stop now") is False
    assert is_stop_command("what does !stop do?") is False
    assert is_stop_command("<@BOT1> what does !stop do?", "BOT1") is False


def test_is_stop_command_rejects_trailing_extra_content():
    assert is_stop_command("<@BOT1> !stop please", "BOT1") is False


def test_is_stop_command_mismatched_mention_not_stripped():
    """A mention that doesn't match bot_id isn't stripped — the leftover text
    (including the stray mention) can't equal "!stop", so this is correctly
    rejected rather than accidentally matching some other user's mention."""
    assert is_stop_command("<@OTHERUSER> !stop", "BOT1") is False


def test_is_stop_command_empty_bot_id_skips_mention_stripping():
    assert is_stop_command("!stop", "") is True
    assert is_stop_command("<@BOT1> !stop", "") is False
