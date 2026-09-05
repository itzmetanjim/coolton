from unittest.mock import Mock

import pytest

from agent import scheduler


@pytest.fixture
def tmp_files(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "REMINDERS_FILE", str(tmp_path / "reminders.json"))
    monkeypatch.setattr(scheduler, "SCHEDULED_TASKS_FILE", str(tmp_path / "scheduled_tasks.json"))
    return tmp_path


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------


def test_schedule_reminder_returns_id_and_saves(tmp_files):
    reminder_id = scheduler.schedule_reminder("U123", "C456", "buy milk", delay_seconds=60)
    assert len(reminder_id) == 8
    data = scheduler._load_reminders()
    assert len(data["reminders"]) == 1
    assert data["reminders"][0]["id"] == reminder_id
    assert data["reminders"][0]["sent"] is False
    assert data["reminders"][0]["user_id"] == "U123"


def test_get_due_reminders_only_due_and_unsent(tmp_files):
    scheduler.schedule_reminder("U1", "C1", "due now", delay_seconds=-10)
    scheduler.schedule_reminder("U2", "C2", "not due yet", delay_seconds=1000)
    due = scheduler._get_due_reminders()
    assert len(due) == 1
    assert due[0]["text"] == "due now"


def test_mark_sent_removes_from_due(tmp_files):
    rid = scheduler.schedule_reminder("U1", "C1", "ping", delay_seconds=-10)
    scheduler._mark_sent(rid)
    assert scheduler._get_due_reminders() == []


def test_load_reminders_missing_or_corrupt(tmp_files):
    assert scheduler._load_reminders() == {"reminders": []}
    (tmp_files / "reminders.json").write_text("{broken")
    assert scheduler._load_reminders() == {"reminders": []}


def test_load_reminders_rejects_non_dict(tmp_files):
    (tmp_files / "reminders.json").write_text("[]")
    assert scheduler._load_reminders() == {"reminders": []}


def test_get_due_reminders_tolerates_a_legacy_entry_missing_fields(tmp_files):
    """A hand-edited or otherwise legacy reminders.json entry missing "sent"/
    "due_at" must not raise — _get_due_reminders runs inside the recurring
    check_reminders job with no surrounding try/except at this level, so one
    bad entry used to silently disable reminders on every run forever."""
    scheduler.schedule_reminder("U1", "C1", "normal one", delay_seconds=-10)
    data = scheduler._load_reminders()
    data["reminders"].append({"id": "legacy", "user_id": "U2", "channel_id": "C2", "text": "old"})
    scheduler._save_reminders(data)

    due = scheduler._get_due_reminders()
    assert [r["text"] for r in due] == ["normal one"]


def test_prune_sent_reminders_tolerates_a_legacy_entry_missing_fields(tmp_files):
    data = {"reminders": [{"id": "legacy", "user_id": "U2", "channel_id": "C2", "text": "old"}]}
    scheduler._prune_sent_reminders(data)  # must not raise
    assert data["reminders"] == [{"id": "legacy", "user_id": "U2", "channel_id": "C2", "text": "old"}]


# ---------------------------------------------------------------------------
# Cron validation
# ---------------------------------------------------------------------------


def test_validate_cron_valid():
    ok, err, next_run = scheduler._validate_cron("0 9 * * *", "UTC")
    assert ok is True
    assert err == ""
    assert isinstance(next_run, float)


def test_validate_cron_bad_timezone():
    ok, err, next_run = scheduler._validate_cron("0 9 * * *", "Not/AZone")
    assert ok is False
    assert "Invalid timezone" in err
    assert next_run is None


def test_validate_cron_too_frequent():
    ok, err, next_run = scheduler._validate_cron("* * * * *", "UTC")
    assert ok is False
    assert "Refusing" in err
    assert next_run is None


def test_validate_cron_half_hour_step_ok():
    ok, err, _ = scheduler._validate_cron("*/30 * * * *", "UTC")
    assert ok is True, err


# ---------------------------------------------------------------------------
# Scheduled task CRUD
# ---------------------------------------------------------------------------

OWNER = "UOWNER"
OTHER = "UOTHER"


def test_create_scheduled_task_valid(tmp_files):
    msg = scheduler.create_scheduled_task(OWNER, "C1", "1.1", "post update", "0 9 * * *", "UTC")
    assert msg.startswith("Created scheduled task ")
    tasks = scheduler._load_tasks()["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["paused"] is False
    assert tasks[0]["channel_id"] == "C1"
    assert tasks[0]["next_run_at"] is not None


def test_create_scheduled_task_validation(tmp_files):
    assert "prompt is required" in scheduler.create_scheduled_task(OWNER, "C1", "1.1", "  ", "0 9 * * *")
    assert "cron expression is required" in scheduler.create_scheduled_task(OWNER, "C1", "1.1", "x", "  ")
    assert "prompt too long" in scheduler.create_scheduled_task(OWNER, "C1", "1.1", "x" * 2001, "0 9 * * *")
    assert "Invalid cron" in scheduler.create_scheduled_task(OWNER, "C1", "1.1", "x", "not-a-cron")


def test_list_scheduled_tasks_scoped_by_user(tmp_files):
    scheduler.create_scheduled_task(OWNER, "C1", "1.1", "mine", "0 9 * * *")
    scheduler.create_scheduled_task(OTHER, "C2", "2.2", "theirs", "0 9 * * *")

    own_listing = scheduler.list_scheduled_tasks(OWNER)
    assert "mine" in own_listing
    assert "theirs" not in own_listing

    # An admin who doesn't ask to see everyone's tasks still only sees their own
    # (per the tool's own docstring: "Only admins CAN view everyone's tasks" —
    # view_all is what opts into that, not implicit admin status alone).
    admin_default_listing = scheduler.list_scheduled_tasks("U0B2VTYER33")
    assert "theirs" not in admin_default_listing

    admin_listing = scheduler.list_scheduled_tasks("U0B2VTYER33", view_all=True)
    assert "mine" in admin_listing
    assert "theirs" in admin_listing


def test_list_scheduled_tasks_view_all_ignored_for_non_admins(tmp_files):
    """view_all must not let a non-admin see other users' tasks — the docstring
    promises "non-admins are ignored", but the check used to be `or` instead of
    `and`, so any caller could pass view_all=True to bypass the per-user scoping."""
    scheduler.create_scheduled_task(OWNER, "C1", "1.1", "mine", "0 9 * * *")
    scheduler.create_scheduled_task(OTHER, "C2", "2.2", "theirs", "0 9 * * *")

    listing = scheduler.list_scheduled_tasks(OWNER, view_all=True)
    assert "mine" in listing
    assert "theirs" not in listing


def test_get_owned_task_permissions(tmp_files):
    scheduler.create_scheduled_task(OWNER, "C1", "1.1", "mine", "0 9 * * *")
    task_id = scheduler._load_tasks()["tasks"][0]["id"]

    task, err = scheduler._get_owned_task(OWNER, task_id)
    assert task is not None and err == ""

    _, err = scheduler._get_owned_task(OTHER, task_id)
    assert "only manage tasks you created" in err

    _, err = scheduler._get_owned_task(OWNER, "missing-id")
    assert "No scheduled task" in err


def test_pause_resume_delete(tmp_files):
    scheduler.create_scheduled_task(OWNER, "C1", "1.1", "mine", "0 9 * * *")
    task_id = scheduler._load_tasks()["tasks"][0]["id"]

    assert scheduler.pause_scheduled_task(OWNER, task_id) == f"Paused scheduled task {task_id}."
    assert scheduler._load_tasks()["tasks"][0]["paused"] is True

    assert scheduler.resume_scheduled_task(OWNER, task_id) == f"Resumed scheduled task {task_id}."
    assert scheduler._load_tasks()["tasks"][0]["paused"] is False

    assert scheduler.delete_scheduled_task(OWNER, task_id) == f"Deleted scheduled task {task_id}."
    assert scheduler._load_tasks()["tasks"] == []


def test_resume_actually_reregisters_the_apscheduler_job(tmp_files, monkeypatch):
    """resume_scheduled_task must re-add the job with APScheduler, not just flip
    the paused flag on disk — the resumed task otherwise stays dormant until
    the next full restart, while `resume_scheduled_task_tool` reports success."""
    scheduler.create_scheduled_task(OWNER, "C1", "1.1", "mine", "0 9 * * *")
    task_id = scheduler._load_tasks()["tasks"][0]["id"]

    fake_scheduler = Mock()
    fake_scheduler.get_jobs.return_value = []
    monkeypatch.setattr(scheduler, "_scheduler", fake_scheduler)

    scheduler.pause_scheduled_task(OWNER, task_id)
    fake_scheduler.add_job.reset_mock()

    scheduler.resume_scheduled_task(OWNER, task_id)

    fake_scheduler.add_job.assert_called_once()
    call_kwargs = fake_scheduler.add_job.call_args.kwargs
    assert call_kwargs.get("id") == f"scheduled_task:{task_id}"


def test_start_scheduler_registers_fallback_cache_refresh_job(monkeypatch, tmp_files):
    """The redesigned fallback cache relies on a periodic background refresh —
    verify start_scheduler actually registers it, at the documented interval,
    with an immediate first run (next_run_time) so the cache is warm from
    process start rather than empty for the first REFRESH_INTERVAL_SECONDS."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    fake_scheduler = Mock()
    monkeypatch.setattr(
        "apscheduler.schedulers.background.BackgroundScheduler", lambda: fake_scheduler
    )
    monkeypatch.setattr(scheduler, "_sync_cron_jobs", lambda: None)

    scheduler.start_scheduler(app=Mock())

    from agent.fallback_cache import REFRESH_INTERVAL_SECONDS

    refresh_calls = [
        c for c in fake_scheduler.add_job.call_args_list
        if c.kwargs.get("id") == "refresh_fallback_cache"
    ]
    assert len(refresh_calls) == 1
    call = refresh_calls[0]
    assert call.kwargs.get("seconds") == REFRESH_INTERVAL_SECONDS
    assert call.kwargs.get("next_run_time") is not None


def test_start_scheduler_registers_mcp_health_refresh_job(monkeypatch, tmp_files):
    """MCP-down alerting (agent/mcp_health.py) depends on this job actually being
    registered — verify start_scheduler wires it up at the documented interval,
    with an immediate first run so a dead MCP connection is caught from process
    start rather than after the first REFRESH_INTERVAL_SECONDS."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    fake_scheduler = Mock()
    monkeypatch.setattr(
        "apscheduler.schedulers.background.BackgroundScheduler", lambda: fake_scheduler
    )
    monkeypatch.setattr(scheduler, "_sync_cron_jobs", lambda: None)

    scheduler.start_scheduler(app=Mock())

    from agent.mcp_health import REFRESH_INTERVAL_SECONDS

    mcp_calls = [
        c for c in fake_scheduler.add_job.call_args_list
        if c.kwargs.get("id") == "refresh_mcp_health"
    ]
    assert len(mcp_calls) == 1
    call = mcp_calls[0]
    assert call.kwargs.get("seconds") == REFRESH_INTERVAL_SECONDS
    assert call.kwargs.get("next_run_time") is not None


def test_cannot_pause_other_users_task(tmp_files):
    scheduler.create_scheduled_task(OWNER, "C1", "1.1", "mine", "0 9 * * *")
    task_id = scheduler._load_tasks()["tasks"][0]["id"]
    assert "only manage tasks you created" in scheduler.pause_scheduled_task(OTHER, task_id)


# ---------------------------------------------------------------------------
# _format_ts
# ---------------------------------------------------------------------------


def test_format_ts_utc():
    # 2020-01-02 01:04:05 UTC
    assert scheduler._format_ts(1577927045) == "2020-01-02 01:04 UTC"


def test_format_ts_none_or_zero():
    assert scheduler._format_ts(None) == "n/a"
    assert scheduler._format_ts(0) == "n/a"


def _fake_web_client(monkeypatch, post_result=None, post_error=None):
    """Stand in for slack_sdk.WebClient in _fire_task: records chat_postMessage
    calls and returns/raises what the test wants, without a real network call."""
    calls = []

    class FakeClient:
        def __init__(self, token=None):
            self.token = token

        def chat_postMessage(self, **kwargs):
            calls.append(kwargs)
            if post_error is not None:
                raise post_error
            return post_result if post_result is not None else {"ok": True, "ts": "999.1"}

    monkeypatch.setattr(scheduler, "WebClient", FakeClient)
    return calls


def test_fire_task_posts_a_banner_and_updates_next_run(tmp_files, monkeypatch):
    scheduler.create_scheduled_task(OWNER, "C1", "1.1", "daily", "0 9 * * *")
    task_id = scheduler._load_tasks()["tasks"][0]["id"]
    monkeypatch.setattr(scheduler, "_scheduler", None)  # ensure no cron job registration
    monkeypatch.setattr(scheduler._task_executor, "submit", lambda *a, **k: None)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    calls = _fake_web_client(monkeypatch)

    scheduler._fire_task(task_id)

    assert len(calls) == 1
    assert calls[0]["channel"] == "C1"
    assert calls[0]["thread_ts"] == "1.1"
    assert "Scheduled task" in calls[0]["text"]

    task = scheduler._load_tasks()["tasks"][0]
    assert task["last_run_at"] is not None
    assert task["next_run_at"] is not None


def test_fire_task_runs_a_real_turn_threaded_off_the_banner(tmp_files, monkeypatch):
    """The bug being fixed: firing used to stop after posting the banner —
    both listeners.events.message and .app_mentioned drop every bot_id
    message, so the prompt never actually ran anything. It must now submit a
    real turn to the task executor."""
    scheduler.create_scheduled_task(OWNER, "C1", "", "do the thing", "0 9 * * *")
    task_id = scheduler._load_tasks()["tasks"][0]["id"]
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    _fake_web_client(monkeypatch, post_result={"ok": True, "ts": "555.1"})

    submitted = []
    monkeypatch.setattr(scheduler._task_executor, "submit", lambda fn, *a: submitted.append((fn, a)))

    scheduler._fire_task(task_id)

    assert len(submitted) == 1
    fn, args = submitted[0]
    assert fn is scheduler._run_scheduled_turn
    client, channel_id, thread_ts, message_ts, user_id, prompt = args
    assert channel_id == "C1"
    # No origin thread_ts was given, so the banner's own ts starts the thread.
    assert thread_ts == "555.1"
    assert message_ts == "555.1"
    assert user_id == OWNER
    assert prompt == "do the thing"


def test_fire_task_uses_the_origin_thread_ts_when_the_task_has_one(tmp_files, monkeypatch):
    scheduler.create_scheduled_task(OWNER, "C1", "1.1", "daily", "0 9 * * *")
    task_id = scheduler._load_tasks()["tasks"][0]["id"]
    monkeypatch.setattr(scheduler, "_scheduler", None)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    _fake_web_client(monkeypatch, post_result={"ok": True, "ts": "555.1"})

    submitted = []
    monkeypatch.setattr(scheduler._task_executor, "submit", lambda fn, *a: submitted.append((fn, a)))

    scheduler._fire_task(task_id)

    _, args = submitted[0]
    assert args[2] == "1.1"  # thread_ts: the task's own origin thread, not the banner's ts


def test_fire_task_no_advance_and_no_turn_when_the_banner_post_fails(tmp_files, monkeypatch):
    from slack_sdk.errors import SlackApiError

    scheduler.create_scheduled_task(OWNER, "C1", "1.1", "daily", "0 9 * * *")
    task_id = scheduler._load_tasks()["tasks"][0]["id"]
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    _fake_web_client(monkeypatch, post_error=SlackApiError("not_in_channel", {"ok": False, "error": "not_in_channel"}))

    submitted = []
    monkeypatch.setattr(scheduler._task_executor, "submit", lambda *a, **k: submitted.append(a))

    scheduler._fire_task(task_id)

    task = scheduler._load_tasks()["tasks"][0]
    assert task["last_run_at"] is None
    assert submitted == []


def test_fire_task_banned_owner_is_skipped_entirely(tmp_files, monkeypatch):
    scheduler.create_scheduled_task(OWNER, "C1", "1.1", "daily", "0 9 * * *")
    task_id = scheduler._load_tasks()["tasks"][0]["id"]
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr("agent.ban_store.is_banned", lambda uid: uid == OWNER)
    calls = _fake_web_client(monkeypatch)

    scheduler._fire_task(task_id)

    assert calls == []
    task = scheduler._load_tasks()["tasks"][0]
    assert task["last_run_at"] is None


def test_fire_task_missing_task_is_noop(tmp_files):
    scheduler._fire_task("does-not-exist")  # should not raise


def test_create_scheduled_task_enforces_the_per_user_cap(tmp_files):
    for i in range(scheduler.MAX_TASKS_PER_USER):
        msg = scheduler.create_scheduled_task(OWNER, "C1", "1.1", f"task {i}", "0 9 * * *")
        assert msg.startswith("Created scheduled task ")

    msg = scheduler.create_scheduled_task(OWNER, "C1", "1.1", "one too many", "0 9 * * *")
    assert f"you already have {scheduler.MAX_TASKS_PER_USER}" in msg
    assert len(scheduler._load_tasks()["tasks"]) == scheduler.MAX_TASKS_PER_USER

    # Cap is per-user — a different user is unaffected.
    other_msg = scheduler.create_scheduled_task(OTHER, "C1", "1.1", "theirs", "0 9 * * *")
    assert other_msg.startswith("Created scheduled task ")
