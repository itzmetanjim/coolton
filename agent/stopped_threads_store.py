"""Threads coolton has been told to stop responding in.

RFC i rule 2 (canvas: "RFC i - Guidelines for AI agents in slack"): when a
message in a thread matches `@<botname> !stop`, the agent must stop responding
in that thread immediately and ignore ALL later messages in it. The stopped
state persists for the lifetime of the process — there is no un-stop; open a
new thread (or restart the process) to talk to coolton there again.

In-memory by design, matching coolton's other stores (stop_store,
active_runs). Both handlers check this BEFORE command parsing, !stop
handling, consent, steering, or any LLM call.
"""

import threading

_lock = threading.Lock()
_stopped_threads: set[tuple[str, str]] = set()


def stop_key(channel_id: str, thread_ts: str | None) -> str:
    """The key identifying "the thread" a stop applies to.

    Handlers pass the RAW event thread_ts (None when the message isn't a
    thread reply). A message with no thread_ts is its own thread root — for a
    DM that means the whole DM, so keying on the channel id is what makes the
    stop actually stick (every DM message would otherwise carry its own ts as
    the thread key and never match).
    """
    return thread_ts or channel_id


def mark_thread_stopped(channel_id: str, thread_ts: str | None) -> None:
    """Ignore all future messages in this thread for the process's lifetime."""
    with _lock:
        _stopped_threads.add((channel_id, stop_key(channel_id, thread_ts)))


def is_thread_stopped(channel_id: str, thread_ts: str | None) -> bool:
    """True once a `!stop` was issued in this thread (and never un-stops)."""
    with _lock:
        return (channel_id, stop_key(channel_id, thread_ts)) in _stopped_threads
