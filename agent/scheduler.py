import json
import os
import logging
import uuid
import time
import threading
# datetime used for typing if needed

logger = logging.getLogger(__name__)

REMINDERS_FILE = "reminders.json"
reminders_lock = threading.Lock()

_scheduler = None


def _load_reminders() -> dict:
    try:
        with open(REMINDERS_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "reminders" not in data:
            return {"reminders": []}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"reminders": []}


def _save_reminders(data: dict):
    temp = f"{REMINDERS_FILE}.tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, REMINDERS_FILE)


def schedule_reminder(user_id: str, channel_id: str, text: str, delay_seconds: int) -> str:
    reminder_id = str(uuid.uuid4())[:8]
    due_at = time.time() + delay_seconds
    with reminders_lock:
        data = _load_reminders()
        data["reminders"].append({
            "id": reminder_id,
            "user_id": user_id,
            "channel_id": channel_id,
            "text": text,
            "due_at": due_at,
            "sent": False,
        })
        _save_reminders(data)
    return reminder_id


def _get_due_reminders() -> list[dict]:
    now = time.time()
    with reminders_lock:
        data = _load_reminders()
        due = [r for r in data["reminders"] if not r["sent"] and r["due_at"] <= now]
        return due


def _mark_sent(reminder_id: str):
    with reminders_lock:
        data = _load_reminders()
        for r in data["reminders"]:
            if r["id"] == reminder_id:
                r["sent"] = True
                break
        _save_reminders(data)


# ---------------------------------------------------------------------------
# Recurring scheduled tasks (cron). Built on the same BackgroundScheduler.
# ---------------------------------------------------------------------------

SCHEDULED_TASKS_FILE = "scheduled_tasks.json"
scheduled_tasks_lock = threading.Lock()
MIN_SCHEDULE_INTERVAL_SECONDS = 30 * 60
ADMIN_USER_IDS = {"U0B2VTYER33", "U09ASUK57K8", "U0BFB1AEY3D", "U0BDCU34308"}


def _load_tasks() -> dict:
    try:
        with open(SCHEDULED_TASKS_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "tasks" not in data:
            return {"tasks": []}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"tasks": []}


def _save_tasks(data: dict):
    temp = f"{SCHEDULED_TASKS_FILE}.tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, SCHEDULED_TASKS_FILE)


def _resolve_tz(timezone: str):
    import zoneinfo
    return zoneinfo.ZoneInfo(timezone)


def _validate_cron(cron: str, timezone: str) -> tuple[bool, str, float | None]:
    """Validate a cron expression and return the next fire time (epoch float)."""
    try:
        tz = _resolve_tz(timezone)
    except Exception:
        return False, f"Invalid timezone '{timezone}'. Use an IANA name like UTC or Asia/Dhaka.", None

    from datetime import datetime
    from croniter import croniter

    try:
        iterator = croniter(cron, datetime.now(tz))
        t1 = iterator.get_next(float)
        t2 = iterator.get_next(float)
    except (ValueError, KeyError, Exception) as e:
        return False, f"Invalid cron expression '{cron}': {e}", None

    if (t2 - t1) < MIN_SCHEDULE_INTERVAL_SECONDS:
        mins = int(MIN_SCHEDULE_INTERVAL_SECONDS / 60)
        return False, (
            f"Refusing '{cron}': it fires more often than every {mins} minutes. "
            f"Use a wider step (e.g. '0 */{mins} * * * *') or a less frequent schedule."
        ), None
    return True, "", t1


def _add_cron_job(task: dict):
    if _scheduler is None:
        return
    from apscheduler.triggers.cron import CronTrigger
    try:
        tz = _resolve_tz(task.get("timezone", "UTC"))
    except Exception:
        tz = None
    job_id = f"scheduled_task:{task['id']}"
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass
    if task.get("paused"):
        return
    trigger = CronTrigger.from_crontab(task["cron"], timezone=tz)
    _scheduler.add_job(
        _fire_task, trigger, args=[task["id"]], id=job_id, replace_existing=True
    )


def _sync_cron_jobs():
    """Reconcile APScheduler jobs with the stored tasks (safe to call anytime)."""
    if _scheduler is None:
        return
    with scheduled_tasks_lock:
        data = _load_tasks()
        stored_ids = {t["id"] for t in data["tasks"]}
    for job in _scheduler.get_jobs():
        if job.id.startswith("scheduled_task:"):
            task_id = job.id.split(":", 1)[1]
            if task_id not in stored_ids:
                try:
                    _scheduler.remove_job(job.id)
                except Exception:
                    pass
    with scheduled_tasks_lock:
        for task in _load_tasks()["tasks"]:
            _add_cron_job(task)


def _fire_task(task_id: str):
    """Post a scheduled task's prompt to its origin thread when cron fires."""
    with scheduled_tasks_lock:
        data = _load_tasks()
        task = next((t for t in data["tasks"] if t["id"] == task_id), None)
        if not task or task.get("paused"):
            return
    try:
        import requests
        bot_token = os.environ.get("SLACK_BOT_TOKEN")
        if not bot_token:
            logger.error("Scheduled task %s: no bot token", task_id)
            return
        payload = {"channel": task["channel_id"], "text": f":timer_clock: *Scheduled task:* {task['prompt']}"}
        if task.get("thread_ts"):
            payload["thread_ts"] = task["thread_ts"]
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            json=payload,
            headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json; charset=utf-8"},
            timeout=20,
        )
        res_json = response.json()
        if res_json.get("ok"):
            with scheduled_tasks_lock:
                data = _load_tasks()
                updated = next((t for t in data["tasks"] if t["id"] == task_id), None)
                if updated:
                    updated["last_run_at"] = time.time()
                    tz = updated.get("timezone", "UTC")
                    try:
                        from datetime import datetime
                        from croniter import croniter
                        updated["next_run_at"] = croniter(updated["cron"], datetime.now(_resolve_tz(tz))).get_next(float)
                    except Exception:
                        updated["next_run_at"] = None
                    _save_tasks(data)
            logger.info("Scheduled task %s fired to %s", task_id, task["channel_id"])
        else:
            logger.error("Scheduled task %s post failed: %s", task_id, res_json.get("error", "unknown"))
    except Exception as e:
        logger.error("Scheduled task %s failed: %s", task_id, e)


def create_scheduled_task(
    user_id: str, channel_id: str, thread_ts: str, prompt: str,
    cron: str, timezone: str = "UTC",
) -> str:
    """Create a recurring cron task that posts `prompt` to the origin thread/channel."""
    if not prompt or not prompt.strip():
        return "Error: prompt is required."
    if not cron or not cron.strip():
        return "Error: cron expression is required."
    if len(prompt) > 2000:
        return "Error: prompt too long (max 2000 chars)."
    ok, err, next_run = _validate_cron(cron.strip(), timezone)
    if not ok:
        return f"Error: {err}"
    task_id = str(uuid.uuid4())[:8]
    task = {
        "id": task_id,
        "user_id": user_id,
        "channel_id": channel_id,
        "thread_ts": thread_ts or "",
        "prompt": prompt.strip(),
        "cron": cron.strip(),
        "timezone": timezone,
        "paused": False,
        "created_at": time.time(),
        "last_run_at": None,
        "next_run_at": next_run,
    }
    with scheduled_tasks_lock:
        data = _load_tasks()
        data["tasks"].append(task)
        _save_tasks(data)
    _add_cron_job(task)
    return f"Created scheduled task {task_id} (cron '{task['cron']}' tz {timezone}). Next run: {_format_ts(next_run)}"


def _format_ts(ts: float | None) -> str:
    if not ts:
        return "n/a"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def list_scheduled_tasks(user_id: str, view_all: bool = False) -> str:
    """List scheduled tasks. Non-admins only see their own."""
    with scheduled_tasks_lock:
        data = _load_tasks()
        if view_all or user_id in ADMIN_USER_IDS:
            tasks = data["tasks"]
        else:
            tasks = [t for t in data["tasks"] if t["user_id"] == user_id]
    if not tasks:
        return "No scheduled tasks found."
    lines = []
    for t in sorted(tasks, key=lambda x: x.get("created_at", 0)):
        status = "paused" if t.get("paused") else "active"
        thread = f" thread {t['thread_ts']}" if t.get("thread_ts") else ""
        lines.append(
            f"- `{t['id']}` [{status}] cron '{t['cron']}' tz {t.get('timezone', 'UTC')} "
            f"→ {t['channel_id']}{thread}\n"
            f"  prompt: {t['prompt'][:120]}\n"
            f"  next: {_format_ts(t.get('next_run_at'))} | last: {_format_ts(t.get('last_run_at'))}"
        )
    return "\n".join(lines)


def _get_owned_task(user_id: str, task_id: str) -> tuple[str | None, str]:
    """Fetch a task by id, enforcing that the caller owns it (or is admin)."""
    with scheduled_tasks_lock:
        data = _load_tasks()
        task = next((t for t in data["tasks"] if t["id"] == task_id), None)
    if not task:
        return None, f"Error: No scheduled task with id '{task_id}'."
    if task["user_id"] != user_id and user_id not in ADMIN_USER_IDS:
        return None, "Error: You can only manage tasks you created."
    return task, ""


def pause_scheduled_task(user_id: str, task_id: str) -> str:
    task, err = _get_owned_task(user_id, task_id)
    if err:
        return err
    with scheduled_tasks_lock:
        data = _load_tasks()
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["paused"] = True
                break
        _save_tasks(data)
    if _scheduler:
        try:
            _scheduler.remove_job(f"scheduled_task:{task_id}")
        except Exception:
            pass
    return f"Paused scheduled task {task_id}."


def resume_scheduled_task(user_id: str, task_id: str) -> str:
    task, err = _get_owned_task(user_id, task_id)
    if err:
        return err
    with scheduled_tasks_lock:
        data = _load_tasks()
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["paused"] = False
                break
        _save_tasks(data)
    _add_cron_job(task)
    return f"Resumed scheduled task {task_id}."


def delete_scheduled_task(user_id: str, task_id: str) -> str:
    task, err = _get_owned_task(user_id, task_id)
    if err:
        return err
    with scheduled_tasks_lock:
        data = _load_tasks()
        data["tasks"] = [t for t in data["tasks"] if t["id"] != task_id]
        _save_tasks(data)
    if _scheduler:
        try:
            _scheduler.remove_job(f"scheduled_task:{task_id}")
        except Exception:
            pass
    return f"Deleted scheduled task {task_id}."


def start_scheduler(app):
    global _scheduler
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError:
        logger.warning("APScheduler not installed — reminders disabled")
        return

    slack_bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not slack_bot_token:
        logger.warning("SLACK_BOT_TOKEN not set — reminders disabled")
        return

    _scheduler = BackgroundScheduler()

    def check_reminders():
        import requests
        due = _get_due_reminders()
        for reminder in due:
            try:
                resp = requests.post(
                    "https://slack.com/api/chat.postMessage",
                    json={
                        "channel": reminder["user_id"],
                        "text": f":alarm_clock: *Reminder:* {reminder['text']}",
                    },
                    headers={
                        "Authorization": f"Bearer {slack_bot_token}",
                        "Content-Type": "application/json",
                    },
                    timeout=20,
                )
                res_json = resp.json()
                if not res_json.get("ok"):
                    logger.error("Failed to send reminder %s: %s", reminder["id"], res_json.get("error", "unknown"))
                    continue
                _mark_sent(reminder["id"])
                logger.info("Sent reminder %s to user %s", reminder["id"], reminder["user_id"])
            except Exception as e:
                logger.error("Failed to send reminder %s: %s", reminder["id"], e)

    def check_token_rotation():
        try:
            from agent.token_rotation import check_and_rotate

            check_and_rotate()
        except Exception:
            logger.exception("Token rotation check failed")

    _scheduler.add_job(check_reminders, "interval", seconds=30, id="check_reminders")
    _scheduler.add_job(check_token_rotation, "interval", seconds=15 * 60, id="check_token_rotation")
    _scheduler.start()
    _sync_cron_jobs()
    logger.info("Reminder scheduler started")
