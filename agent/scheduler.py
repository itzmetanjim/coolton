import json
import os
import logging
import uuid
import time
import threading
from concurrent.futures import ThreadPoolExecutor
# datetime used for typing if needed

from slack_sdk import WebClient

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


REMINDER_RETENTION_SECONDS = 7 * 86400


def _prune_sent_reminders(data: dict) -> None:
    """Drop reminders sent more than a week ago so reminders.json doesn't grow
    forever — every reminder ever created otherwise stays in the file (and gets
    re-scanned every 30s by check_reminders) indefinitely.

    Uses .get() rather than direct indexing: this (and _get_due_reminders,
    same reasoning) runs inside the recurring check_reminders job with no
    surrounding try/except at this level — one legacy or hand-edited entry
    missing "sent"/"due_at" would otherwise raise KeyError and silently
    disable reminders on every single run from then on, forever, not just
    skip that one bad entry.
    """
    now = time.time()
    data["reminders"] = [
        r for r in data["reminders"]
        if not r.get("sent") or now - r.get("due_at", now) < REMINDER_RETENTION_SECONDS
    ]


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
        _prune_sent_reminders(data)
        _save_reminders(data)
    return reminder_id


def _get_due_reminders() -> list[dict]:
    now = time.time()
    with reminders_lock:
        data = _load_reminders()
        due = [r for r in data["reminders"] if not r.get("sent") and r.get("due_at", float("inf")) <= now]
        return due


def _mark_sent(reminder_id: str):
    with reminders_lock:
        data = _load_reminders()
        for r in data["reminders"]:
            if r["id"] == reminder_id:
                r["sent"] = True
                break
        _prune_sent_reminders(data)
        _save_reminders(data)


# ---------------------------------------------------------------------------
# Recurring scheduled tasks (cron). Built on the same BackgroundScheduler.
# ---------------------------------------------------------------------------

SCHEDULED_TASKS_FILE = "scheduled_tasks.json"
scheduled_tasks_lock = threading.Lock()
MIN_SCHEDULE_INTERVAL_SECONDS = 30 * 60
# Firing had no effect at all until now (see _fire_task) — nothing bounded how
# many a single user could pile up. Now that firing actually runs a real
# agent turn, an unbounded pile of tasks is an unbounded pile of autonomous
# turns; cap it per user.
MAX_TASKS_PER_USER = 20
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
            f"Use a wider step (e.g. '*/{mins} * * * *') or a less frequent schedule."
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


# Runs each fired task's real agent turn off of APScheduler's own worker
# thread, so a slow/long turn can never block other cron jobs (including other
# users' scheduled tasks) from firing on time.
_task_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="scheduled-task")


def _fire_task(task_id: str):
    """When cron fires: post a banner announcing the task, then run its prompt
    as a real agent turn threaded off that banner (or the task's own origin
    thread, if it had one).

    Previously this only posted the banner via chat.postMessage and stopped —
    both listeners.events.message and .app_mentioned drop every message with
    a bot_id, so nothing ever consumed it and the prompt silently never ran,
    despite create_scheduled_task_tool advertising a working recurring-task
    feature end to end.
    """
    with scheduled_tasks_lock:
        data = _load_tasks()
        task = next((t for t in data["tasks"] if t["id"] == task_id), None)
        if not task or task.get("paused"):
            return

    from agent.ban_store import is_banned
    if is_banned(task["user_id"]):
        logger.warning("Scheduled task %s: owner %s is banned; not firing", task_id, task["user_id"])
        return

    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token:
        logger.error("Scheduled task %s: no bot token", task_id)
        return

    client = WebClient(token=bot_token)
    try:
        response = client.chat_postMessage(
            channel=task["channel_id"],
            text=f":timer_clock: *Scheduled task:* {task['prompt']}",
            thread_ts=task["thread_ts"] or None,
        )
    except Exception as e:
        logger.error("Scheduled task %s post failed: %s", task_id, e)
        return

    thread_ts = task["thread_ts"] or response["ts"]
    message_ts = response["ts"]

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
    _task_executor.submit(
        _run_scheduled_turn, client, task["channel_id"], thread_ts, message_ts,
        task["user_id"], task["prompt"],
    )


def _run_scheduled_turn(client: WebClient, channel_id: str, thread_ts: str, message_ts: str, user_id: str, prompt: str) -> None:
    """Run a scheduled task's prompt through the exact same turn pipeline a
    real Slack message runs through (listeners.events.turn.run_agent_turn) —
    Say/SayStream are the same helpers Bolt hands a real event listener,
    built directly here since there's no incoming event to hand them to us."""
    from slack_bolt import Say, SayStream

    from listeners.events.turn import run_agent_turn
    from thread_context import conversation_store

    say = Say(client=client, channel=channel_id, thread_ts=thread_ts)
    say_stream = SayStream(client=client, channel=channel_id, thread_ts=thread_ts)
    try:
        run_agent_turn(
            client=client, say=say, say_stream=say_stream, logger=logger,
            channel_id=channel_id, thread_ts=thread_ts, message_ts=message_ts,
            user_id=user_id, user_token=os.environ.get("SLACK_USER_TOKEN"),
            text=prompt, history=conversation_store.get_history(channel_id, thread_ts),
        )
    except Exception:
        logger.exception("Scheduled task turn failed for %s/%s", channel_id, thread_ts)


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
        owned = sum(1 for t in data["tasks"] if t["user_id"] == user_id)
        if owned >= MAX_TASKS_PER_USER:
            return f"Error: you already have {MAX_TASKS_PER_USER} scheduled tasks (the max). Delete or pause one first."
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
        if view_all and user_id in ADMIN_USER_IDS:
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


def _get_owned_task(user_id: str, task_id: str) -> tuple[dict | None, str]:
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
        updated_task = None
        for t in data["tasks"]:
            if t["id"] == task_id:
                t["paused"] = False
                updated_task = t
                break
        _save_tasks(data)
    # Use the just-updated (paused=False) task, not the stale copy from
    # _get_owned_task's earlier _load_tasks() snapshot — passing the stale one
    # (still paused=True) makes _add_cron_job silently no-op, so the task
    # reports "resumed" but never actually gets re-registered with APScheduler
    # until the next full restart-time _sync_cron_jobs().
    if updated_task:
        _add_cron_job(updated_task)
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


# ---------------------------------------------------------------------------
# One-off waits: "pause this conversation for N seconds, then let the agent
# keep reasoning in it" — distinct from create_scheduled_task (recurring,
# 30-minute floor) and schedule_reminder (a static DM with no further
# reasoning). Modeled on gorkie's `wait` tool. Built on the same
# BackgroundScheduler, but a "date" trigger (fires once) instead of cron.
# ---------------------------------------------------------------------------

WAIT_TASKS_FILE = "wait_tasks.json"
wait_tasks_lock = threading.Lock()
# Longer than this and it's not really a "wait" anymore — point the model at
# schedule_reminder_tool (one-time, no further reasoning) or
# create_scheduled_task_tool (recurring) instead.
MAX_WAIT_SECONDS = 6 * 3600
WAIT_RETENTION_SECONDS = 7 * 86400


def _load_waits() -> dict:
    try:
        with open(WAIT_TASKS_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "waits" not in data:
            return {"waits": []}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"waits": []}


def _save_waits(data: dict):
    temp = f"{WAIT_TASKS_FILE}.tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, WAIT_TASKS_FILE)


def _prune_fired_waits(data: dict) -> None:
    now = time.time()
    data["waits"] = [
        w for w in data["waits"]
        if not w.get("fired") or now - w.get("fire_at", now) < WAIT_RETENTION_SECONDS
    ]


def _add_wait_job(wait: dict) -> None:
    if _scheduler is None:
        return
    job_id = f"wait:{wait['id']}"
    try:
        _scheduler.remove_job(job_id)
    except Exception:
        pass
    if wait.get("fired"):
        return
    from datetime import datetime, timezone
    run_date = datetime.fromtimestamp(wait["fire_at"], timezone.utc)
    # misfire_grace_time=None: always fire regardless of how overdue — a wait
    # that was already due when the process restarted must still resume the
    # conversation, not silently vanish because it missed some default grace
    # window.
    _scheduler.add_job(
        _fire_wait, "date", run_date=run_date, args=[wait["id"]],
        id=job_id, replace_existing=True, misfire_grace_time=None,
    )


def _sync_wait_jobs():
    """Reconcile APScheduler jobs with stored waits (safe to call anytime) —
    same pattern as _sync_cron_jobs, so a wait started before a restart still
    fires instead of being lost."""
    if _scheduler is None:
        return
    with wait_tasks_lock:
        data = _load_waits()
    for wait in data["waits"]:
        if not wait.get("fired"):
            _add_wait_job(wait)


def _fire_wait(wait_id: str) -> None:
    with wait_tasks_lock:
        data = _load_waits()
        wait = next((w for w in data["waits"] if w["id"] == wait_id), None)
        if not wait or wait.get("fired"):
            return
        wait["fired"] = True
        _prune_fired_waits(data)
        _save_waits(data)

    from agent.ban_store import is_banned
    if is_banned(wait["user_id"]):
        logger.info("Wait %s: owner %s is banned; not firing", wait_id, wait["user_id"])
        return

    channel_id, thread_ts = wait["channel_id"], wait["thread_ts"]
    prompt = (
        f"[SYSTEM: your {int(wait.get('seconds', 0))}s wait is over (you were waiting for: "
        f"{wait['reason']}). Continue and respond in this same conversation.]"
    )
    banner = f":alarm_clock: _wait over (nobody sent this) — {wait['reason']}_"

    from agent.active_runs import is_run_active
    if is_run_active(channel_id, thread_ts):
        # Same rule agent.background_jobs_poller._notify_finished follows: a
        # turn already running on this thread picks the wait-over prompt up
        # as a steering message on its next tool call instead of racing a
        # second turn against it.
        from agent.steering_store import queue_steering_message
        queue_steering_message(channel_id, thread_ts, prompt, user_id="", message_ts="")
        return

    from agent.background_jobs_poller import AUTOMATED_USER_ID, _dispatch_wake, _wake_executor
    _wake_executor.submit(_dispatch_wake, channel_id, thread_ts, AUTOMATED_USER_ID, banner, prompt, wait["user_id"])


def create_wait(user_id: str, channel_id: str, thread_ts: str, reason: str, seconds: int) -> str:
    """Schedule a one-off wake-up of this exact conversation. Returns the
    wait id, or an "Error: ..." string."""
    if seconds <= 0:
        return "Error: seconds must be positive."
    if seconds > MAX_WAIT_SECONDS:
        hours = MAX_WAIT_SECONDS // 3600
        return (
            f"Error: max wait is {MAX_WAIT_SECONDS}s (~{hours}h). For a longer one-time delay use "
            f"schedule_reminder_tool; for anything recurring use create_scheduled_task_tool."
        )
    if not reason or not reason.strip():
        return "Error: reason is required."

    wait_id = str(uuid.uuid4())[:8]
    wait = {
        "id": wait_id,
        "user_id": user_id,
        "channel_id": channel_id,
        "thread_ts": thread_ts or "",
        "reason": reason.strip(),
        "seconds": seconds,
        "fire_at": time.time() + seconds,
        "fired": False,
    }
    with wait_tasks_lock:
        data = _load_waits()
        _prune_fired_waits(data)
        data["waits"].append(wait)
        _save_waits(data)
    _add_wait_job(wait)
    return wait_id


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

    def refresh_fallback_cache_job():
        try:
            from agent.provider_probe import refresh_fallback_cache

            refresh_fallback_cache()
        except Exception:
            logger.exception("Fallback cache background refresh failed")

    def refresh_mcp_health_job():
        try:
            from agent.mcp_health import refresh_mcp_health

            refresh_mcp_health()
        except Exception:
            logger.exception("MCP health background check failed")

    def poll_background_jobs_job():
        try:
            from agent.background_jobs_poller import poll_background_jobs

            poll_background_jobs()
        except Exception:
            logger.exception("Background job polling failed")

    _scheduler.add_job(check_reminders, "interval", seconds=30, id="check_reminders")
    _scheduler.add_job(poll_background_jobs_job, "interval", seconds=5, id="poll_background_jobs")
    _scheduler.add_job(check_token_rotation, "interval", seconds=15 * 60, id="check_token_rotation")
    # Runs once immediately (next_run_time=now) so the cache is warm from
    # process start, then every REFRESH_INTERVAL_SECONDS after that — see
    # agent/fallback_cache.py and agent/provider_probe.py for why.
    from datetime import datetime as _datetime
    from agent.fallback_cache import REFRESH_INTERVAL_SECONDS
    _scheduler.add_job(
        refresh_fallback_cache_job, "interval", seconds=REFRESH_INTERVAL_SECONDS,
        id="refresh_fallback_cache", next_run_time=_datetime.now(),
    )
    from agent.mcp_health import REFRESH_INTERVAL_SECONDS as MCP_HEALTH_INTERVAL_SECONDS
    _scheduler.add_job(
        refresh_mcp_health_job, "interval", seconds=MCP_HEALTH_INTERVAL_SECONDS,
        id="refresh_mcp_health", next_run_time=_datetime.now(),
    )
    _scheduler.start()
    _sync_cron_jobs()
    _sync_wait_jobs()
    logger.info("Reminder scheduler started")
