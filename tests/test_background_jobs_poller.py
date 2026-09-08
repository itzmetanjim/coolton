"""agent.background_jobs_poller — the periodic check that notices a
run_background_command job finishing and reacts to it: folds it into a live
run as a steering message if one is active, otherwise starts a fresh turn
(Slack banner + real turn, or web.runner.wake_conversation)."""

import time
from unittest.mock import Mock

import pytest

from agent import background_jobs_poller as poller


def _job(**kw):
    base = {
        "job_id": "abcd1234", "channel_id": "C1", "thread_ts": "1.1",
        "user_id": "U1", "command": "npm run build", "started_at": time.time(),
    }
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def not_banned(monkeypatch):
    monkeypatch.setattr("agent.ban_store.is_banned", lambda uid: False)


# ---------------------------------------------------------------------------
# poll_background_jobs / _poll_one
# ---------------------------------------------------------------------------


def test_poll_background_jobs_skips_still_running_jobs(monkeypatch):
    monkeypatch.setattr("agent.background_jobs_store.list_jobs", lambda: [_job()])
    monkeypatch.setattr("agent.tools.sandbox_background.get_job_status", lambda *a, **k: ("RUNNING", "..."))
    notified = []
    monkeypatch.setattr(poller, "_notify_finished", lambda job, output: notified.append(job))
    poller.poll_background_jobs()
    assert notified == []


def test_poll_background_jobs_ignores_error_strings(monkeypatch):
    monkeypatch.setattr("agent.background_jobs_store.list_jobs", lambda: [_job()])
    monkeypatch.setattr("agent.tools.sandbox_background.get_job_status", lambda *a, **k: "Error: gone")
    notified = []
    monkeypatch.setattr(poller, "_notify_finished", lambda job, output: notified.append(job))
    poller.poll_background_jobs()
    assert notified == []


def test_poll_background_jobs_notifies_when_finished(monkeypatch):
    monkeypatch.setattr("agent.background_jobs_store.list_jobs", lambda: [_job()])
    monkeypatch.setattr("agent.tools.sandbox_background.get_job_status", lambda *a, **k: ("EXITED", "done!"))
    notified = []
    monkeypatch.setattr(poller, "_notify_finished", lambda job, output: notified.append((job["job_id"], output)))
    poller.poll_background_jobs()
    assert notified == [("abcd1234", "done!")]


def test_poll_background_jobs_one_bad_job_does_not_stop_the_others(monkeypatch):
    monkeypatch.setattr("agent.background_jobs_store.list_jobs", lambda: [_job(job_id="bad"), _job(job_id="good")])

    def fake_status(channel_id, thread_ts, job_id, tail_lines=100):
        if job_id == "bad":
            raise RuntimeError("boom")
        return ("EXITED", "ok")

    monkeypatch.setattr("agent.tools.sandbox_background.get_job_status", fake_status)
    notified = []
    monkeypatch.setattr(poller, "_notify_finished", lambda job, output: notified.append(job["job_id"]))
    poller.poll_background_jobs()
    assert notified == ["good"]


def test_poll_one_abandons_jobs_past_max_age(monkeypatch):
    unregistered = []
    monkeypatch.setattr("agent.background_jobs_store.unregister_job", lambda job_id: unregistered.append(job_id))
    checked = []
    monkeypatch.setattr(
        "agent.tools.sandbox_background.get_job_status",
        lambda *a, **k: checked.append(a) or ("RUNNING", ""),
    )
    old_job = _job(started_at=time.time() - poller._MAX_JOB_AGE_SECONDS - 1)
    poller._poll_one(old_job)
    assert unregistered == ["abcd1234"]
    assert checked == []  # never even checked its status — already given up


# ---------------------------------------------------------------------------
# _notify_finished
# ---------------------------------------------------------------------------


def test_notify_finished_queues_steering_when_a_run_is_active(monkeypatch):
    monkeypatch.setattr("agent.active_runs.is_run_active", lambda c, t: True)
    queued = []
    monkeypatch.setattr("agent.steering_store.queue_steering_message", lambda *a, **k: queued.append((a, k)))
    submitted = []
    monkeypatch.setattr(poller._wake_executor, "submit", lambda *a: submitted.append(a))

    poller._notify_finished(_job(), "build succeeded")

    assert len(queued) == 1
    args, kwargs = queued[0]
    channel_id, thread_ts, text = args[0], args[1], args[2]
    assert (channel_id, thread_ts) == ("C1", "1.1")
    assert "abcd1234" in text
    assert "npm run build" in text
    assert "build succeeded" in text
    assert submitted == []  # no fresh turn — folded into the live one instead


def test_notify_finished_starts_a_fresh_turn_when_nothing_is_active(monkeypatch):
    monkeypatch.setattr("agent.active_runs.is_run_active", lambda c, t: False)
    queued = []
    monkeypatch.setattr("agent.steering_store.queue_steering_message", lambda *a, **k: queued.append(a))
    submitted = []
    monkeypatch.setattr(poller._wake_executor, "submit", lambda *a: submitted.append(a))

    poller._notify_finished(_job(), "build succeeded")

    assert queued == []
    assert len(submitted) == 1
    fn, channel_id, thread_ts, user_id, job_id, command, output = submitted[0]
    assert fn is poller._wake
    assert (channel_id, thread_ts, user_id, job_id, command, output) == (
        "C1", "1.1", "U1", "abcd1234", "npm run build", "build succeeded",
    )


def test_notify_finished_skips_a_banned_owner(monkeypatch):
    monkeypatch.setattr("agent.ban_store.is_banned", lambda uid: True)
    monkeypatch.setattr("agent.active_runs.is_run_active", lambda c, t: False)
    queued = []
    submitted = []
    monkeypatch.setattr("agent.steering_store.queue_steering_message", lambda *a, **k: queued.append(a))
    monkeypatch.setattr(poller._wake_executor, "submit", lambda *a: submitted.append(a))

    poller._notify_finished(_job(), "build succeeded")

    assert queued == []
    assert submitted == []


# ---------------------------------------------------------------------------
# _wake — dispatches to web or Slack by channel_id
# ---------------------------------------------------------------------------


def test_wake_dispatches_to_web_for_the_web_channel(monkeypatch):
    calls = []
    monkeypatch.setattr(poller, "_wake_web", lambda *a: calls.append(("web", a)))
    monkeypatch.setattr(poller, "_wake_slack", lambda *a: calls.append(("slack", a)))
    from web.runner import WEB_CHANNEL_ID

    poller._wake(WEB_CHANNEL_ID, "convo-1", "U1", "abcd1234", "npm run build", "done")
    assert len(calls) == 1
    assert calls[0][0] == "web"


def test_wake_uses_the_automated_sentinel_not_the_real_owner_id(monkeypatch):
    """Nobody actually sent this turn — deps.user_id must not be the job
    owner's real id, or coolton treats it as talking to them again (their
    name, their custom instructions). Applies to both web and Slack."""
    calls = []
    monkeypatch.setattr(poller, "_wake_web", lambda *a: calls.append(("web", a)))
    monkeypatch.setattr(poller, "_wake_slack", lambda *a: calls.append(("slack", a)))
    from web.runner import WEB_CHANNEL_ID

    poller._wake(WEB_CHANNEL_ID, "convo-1", "U1", "abcd1234", "npm run build", "done")
    assert calls[0][1][1] == poller.AUTOMATED_USER_ID
    assert calls[0][1][1] != "U1"

    calls.clear()
    poller._wake("C1", "1.1", "U1", "abcd1234", "npm run build", "done")
    assert calls[0][1][2] == poller.AUTOMATED_USER_ID
    assert calls[0][1][2] != "U1"


def test_wake_dispatches_to_slack_for_a_slack_channel(monkeypatch):
    calls = []
    monkeypatch.setattr(poller, "_wake_web", lambda *a: calls.append(("web", a)))
    monkeypatch.setattr(poller, "_wake_slack", lambda *a: calls.append(("slack", a)))

    poller._wake("C1", "1.1", "U1", "abcd1234", "npm run build", "done")
    assert len(calls) == 1
    assert calls[0][0] == "slack"


def test_wake_banner_makes_clear_it_is_not_from_the_user(monkeypatch):
    """A human reading the channel (or the model itself) must be able to tell
    this wasn't typed by anyone — it's an autonomous check-in."""
    calls = []
    monkeypatch.setattr(poller, "_wake_slack", lambda *a: calls.append(a))
    poller._wake("C1", "1.1", "U1", "abcd1234", "npm run build", "done")
    banner = calls[0][3]
    assert "not" in banner.lower() or "nobody" in banner.lower() or "automatic" in banner.lower()


def test_wake_web_calls_wake_conversation(monkeypatch):
    calls = []
    monkeypatch.setattr("web.runner.wake_conversation", lambda *a: calls.append(a))
    poller._wake_web("convo-1", "U1", "banner text", "prompt text")
    assert calls == [("convo-1", "U1", "banner text", "prompt text")]


def test_wake_web_swallows_errors(monkeypatch):
    monkeypatch.setattr("web.runner.wake_conversation", Mock(side_effect=RuntimeError("boom")))
    poller._wake_web("convo-1", "U1", "banner", "prompt")  # must not raise


def test_wake_slack_posts_a_banner_and_runs_a_real_turn(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    post_calls = []

    class FakeClient:
        def __init__(self, token=None):
            self.token = token

        def chat_postMessage(self, **kwargs):
            post_calls.append(kwargs)
            return {"ok": True, "ts": "999.1"}

    monkeypatch.setattr(poller, "WebClient", FakeClient)
    turn_calls = []
    monkeypatch.setattr(
        "listeners.events.turn.run_agent_turn",
        lambda **kwargs: turn_calls.append(kwargs),
    )

    poller._wake_slack("C1", "1.1", "U1", "banner text", "prompt text")

    assert len(post_calls) == 1
    assert post_calls[0]["channel"] == "C1"
    assert post_calls[0]["thread_ts"] == "1.1"
    assert post_calls[0]["text"] == "banner text"

    assert len(turn_calls) == 1
    kwargs = turn_calls[0]
    assert kwargs["channel_id"] == "C1"
    assert kwargs["thread_ts"] == "1.1"
    assert kwargs["message_ts"] == "999.1"
    assert kwargs["user_id"] == "U1"
    assert kwargs["text"] == "prompt text"


def test_wake_slack_uses_the_banners_own_ts_when_no_thread_ts(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    class FakeClient:
        def __init__(self, token=None):
            pass

        def chat_postMessage(self, **kwargs):
            return {"ok": True, "ts": "555.1"}

    monkeypatch.setattr(poller, "WebClient", FakeClient)
    turn_calls = []
    monkeypatch.setattr("listeners.events.turn.run_agent_turn", lambda **kwargs: turn_calls.append(kwargs))

    poller._wake_slack("C1", "", "U1", "banner", "prompt")

    assert turn_calls[0]["thread_ts"] == "555.1"


def test_wake_slack_without_bot_token_does_nothing(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    called = []
    monkeypatch.setattr(poller, "WebClient", lambda **k: called.append(k))
    poller._wake_slack("C1", "1.1", "U1", "banner", "prompt")
    assert called == []


def test_wake_slack_banner_post_failure_does_not_start_a_turn(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    class FakeClient:
        def __init__(self, token=None):
            pass

        def chat_postMessage(self, **kwargs):
            raise RuntimeError("channel_not_found")

    monkeypatch.setattr(poller, "WebClient", FakeClient)
    turn_calls = []
    monkeypatch.setattr("listeners.events.turn.run_agent_turn", lambda **kwargs: turn_calls.append(kwargs))

    poller._wake_slack("C1", "1.1", "U1", "banner", "prompt")  # must not raise
    assert turn_calls == []
