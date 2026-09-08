"""Host-side registry of outstanding run_background_command jobs.

The job's own state (running/exited, output) lives entirely in the sandbox's
filesystem (see agent/tools/sandbox_background.py) — this file exists only to
answer a question the sandbox itself can't: "which (channel_id, thread_ts,
job_id) triples currently have a background job someone might want to be
notified about?" agent.background_jobs_poller polls exactly this list.
"""

import json
import os
import threading
import time

BACKGROUND_JOBS_FILE = "background_jobs.json"
_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(BACKGROUND_JOBS_FILE, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            return {"jobs": []}
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"jobs": []}


def _save(data: dict) -> None:
    temp = f"{BACKGROUND_JOBS_FILE}.tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, BACKGROUND_JOBS_FILE)


def register_job(job_id: str, channel_id: str, thread_ts: str, user_id: str, command: str) -> None:
    with _lock:
        data = _load()
        data["jobs"].append({
            "job_id": job_id,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "user_id": user_id,
            "command": command,
            "started_at": time.time(),
        })
        _save(data)


def unregister_job(job_id: str) -> None:
    with _lock:
        data = _load()
        remaining = [j for j in data["jobs"] if j.get("job_id") != job_id]
        if len(remaining) != len(data["jobs"]):
            data["jobs"] = remaining
            _save(data)


def list_jobs() -> list[dict]:
    with _lock:
        return list(_load()["jobs"])


def has_pending_jobs(channel_id: str, thread_ts: str) -> bool:
    """True if this thread has a background job still being tracked — used to
    decide whether a sandbox-touching tool should keep the sandbox warm
    (agent.sandbox_keepalive) instead of pausing it, so the job actually gets
    to run instead of freezing the instant it's launched."""
    with _lock:
        return any(
            j.get("channel_id") == channel_id and j.get("thread_ts") == thread_ts
            for j in _load()["jobs"]
        )
