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
    if not os.path.exists(REMINDERS_FILE):
        return {"reminders": []}
    with open(REMINDERS_FILE, "r") as f:
        return json.load(f)


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


def get_user_reminders(user_id: str) -> list[dict]:
    """Get all reminders for a user, sorted by due time."""
    with reminders_lock:
        data = _load_reminders()
        user_reminders = [r for r in data["reminders"] if r["user_id"] == user_id]
        user_reminders.sort(key=lambda r: r["due_at"])
        return user_reminders


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
                requests.post(
                    "https://slack.com/api/chat.postMessage",
                    json={
                        "channel": reminder["user_id"],
                        "text": f":alarm_clock: *Reminder:* {reminder['text']}",
                    },
                    headers={
                        "Authorization": f"Bearer {slack_bot_token}",
                        "Content-Type": "application/json",
                    },
                )
                _mark_sent(reminder["id"])
                logger.info("Sent reminder %s to user %s", reminder["id"], reminder["user_id"])
            except Exception as e:
                logger.error("Failed to send reminder %s: %s", reminder["id"], e)

    _scheduler.add_job(check_reminders, "interval", seconds=30, id="check_reminders")
    _scheduler.start()
    logger.info("Reminder scheduler started")


def stop_scheduler():
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
