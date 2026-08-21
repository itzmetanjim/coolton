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

    admin_listing = scheduler.list_scheduled_tasks("U0B2VTYER33")
    assert "mine" in admin_listing
    assert "theirs" in admin_listing


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


def test_fire_task_posts_and_updates_next_run(tmp_files, monkeypatch):
    scheduler.create_scheduled_task(OWNER, "C1", "1.1", "daily", "0 9 * * *")
    task_id = scheduler._load_tasks()["tasks"][0]["id"]
    monkeypatch.setattr(scheduler, "_scheduler", None)  # ensure no cron job registration

    class FakeResponse:
        def json(self):
            return {"ok": True}

    import requests

    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    scheduler._fire_task(task_id)

    assert len(calls) == 1
    assert calls[0][0] == "https://slack.com/api/chat.postMessage"
    payload = calls[0][1]["json"]
    assert payload["channel"] == "C1"
    assert payload["thread_ts"] == "1.1"
    assert "Scheduled task" in payload["text"]

    task = scheduler._load_tasks()["tasks"][0]
    assert task["last_run_at"] is not None
    assert task["next_run_at"] is not None


def test_fire_task_no_advance_on_failure(tmp_files, monkeypatch):
    scheduler.create_scheduled_task(OWNER, "C1", "1.1", "daily", "0 9 * * *")
    task_id = scheduler._load_tasks()["tasks"][0]["id"]

    import requests

    class FakeResponse:
        def json(self):
            return {"ok": False, "error": "not_in_channel"}

    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResponse())
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    scheduler._fire_task(task_id)

    task = scheduler._load_tasks()["tasks"][0]
    assert task["last_run_at"] is None


def test_fire_task_missing_task_is_noop(tmp_files):
    scheduler._fire_task("does-not-exist")  # should not raise
