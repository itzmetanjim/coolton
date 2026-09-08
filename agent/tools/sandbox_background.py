"""Background process execution in the E2B sandbox — coolton's equivalent of
Claude Code's Bash(run_in_background) + BashOutput + KillShell, for a dev
server, watcher, or long build the model wants to start and keep checking on
instead of blocking a whole turn on run_linux_command.

Job OUTPUT lives entirely in the sandbox's own filesystem (one log file + one
pid file per job, under ~/.coolton_bg/) rather than on the host — the sandbox
already persists across pause/resume for everything else, so a job started
in one turn is still there (running or finished) in a later turn, even
across a full app restart, with no separate host-side store to keep in sync
for that part. agent.background_jobs_store separately tracks just enough
(channel_id, thread_ts, job_id, command, started_at) for
agent.background_jobs_poller to know which threads to check on and who
started what — the sandbox filesystem alone can't answer "which threads have
an outstanding job" without connecting to every sandbox that ever existed.

A job is started with `setsid nohup ... &` on its own line (not combined with
`&&`/`;` on the same line as the `&` — that would background a compound list
and make `$!` capture a wrapping subshell's PID instead of the actual
detached process, which then couldn't be killed later) so it survives the
launching shell exiting and is fully detached from the sandbox's controlling
session.

Pausing an E2B sandbox freezes it (a memory snapshot) — anything running
inside, including a backgrounded job, makes zero progress while paused.
Every other sandbox tool pauses immediately after each call to save cost
while idle, but that would mean a background job gets frozen within
milliseconds of starting and only inches forward during the brief windows
something happens to reconnect to it. So instead, while ANY job is pending
for a thread (background_jobs_store.has_pending_jobs), sandbox-touching tools
keep the sandbox warm via agent.sandbox_keepalive instead of pausing (see
_rearm_or_pause below and run_linux_command's own finally block) — the
poller's periodic re-check re-arms that countdown every cycle for as long as
the job keeps running, so it gets to actually run instead of being throttled
to a couple of seconds every poll interval.
"""

import re
import shlex
import uuid

from agent.sandbox_helpers import get_or_create_sandbox

_JOB_ID_RE = re.compile(r"^[a-f0-9]{8}$")
_BG_DIR = "~/.coolton_bg"
_DEFAULT_TAIL_LINES = 200

# How long the sandbox stays warm after each "still running" check before it
# would auto-pause with nothing re-arming it — comfortably longer than the
# poller's own interval (agent.background_jobs_poller) so normal poll cycles
# always land well inside the window and keep extending it.
BG_JOB_KEEPALIVE_SECONDS = 300


def _get_sandbox(channel_id: str, thread_ts: str):
    try:
        return get_or_create_sandbox(channel_id, thread_ts)[0], None
    except Exception as e:
        return None, f"Error: {e}"


def _paths(job_id: str) -> tuple[str, str]:
    return f"{_BG_DIR}/{job_id}.log", f"{_BG_DIR}/{job_id}.pid"


def _rearm_or_pause(sandbox, channel_id: str, thread_ts: str, still_running: bool) -> None:
    """Keep the sandbox warm if this job (or any other pending job on this
    thread) still needs it to make progress; otherwise pause as usual."""
    from agent import sandbox_keepalive
    from agent.background_jobs_store import has_pending_jobs

    if still_running or has_pending_jobs(channel_id, thread_ts):
        sandbox_keepalive.arm(channel_id, thread_ts, BG_JOB_KEEPALIVE_SECONDS)
    else:
        sandbox.pause()


def run_background_command(channel_id: str, thread_ts: str, command: str, user_id: str, cwd: str = "") -> str:
    """Start `command` in the sandbox detached in the background and return
    immediately with a job id, instead of blocking until it finishes.

    Returns:
        A message with the job id, or an error message.
    """
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err

    job_id = uuid.uuid4().hex[:8]
    log_path, pid_path = _paths(job_id)
    lines = [f"mkdir -p {_BG_DIR}"]
    if cwd:
        lines.append(f"cd {shlex.quote(cwd)}")
    lines.append(f"setsid nohup bash -c {shlex.quote(command)} </dev/null > {log_path} 2>&1 &")
    lines.append(f"echo $! > {pid_path}")
    script = "\n".join(lines)

    try:
        result = sandbox.commands.run(script, timeout=30)
    except Exception as e:
        sandbox.pause()
        return f"Error starting background command: {e}"

    if result.exit_code != 0:
        sandbox.pause()
        return f"Error starting background command (exit {result.exit_code}): {result.stderr}"

    from agent.background_jobs_store import register_job
    register_job(job_id, channel_id, thread_ts, user_id, command)
    _rearm_or_pause(sandbox, channel_id, thread_ts, still_running=True)
    return (
        f"Started background command with id `{job_id}`. Check its output with "
        f'check_background_command_tool(job_id="{job_id}"), stop it with '
        f'kill_background_command_tool(job_id="{job_id}"). You\'ll also be notified '
        f"automatically when it finishes — no need to poll check_background_command_tool "
        f"just to wait."
    )


def get_job_status(channel_id: str, thread_ts: str, job_id: str, tail_lines: int = _DEFAULT_TAIL_LINES):
    """Check one job's status and recent output, updating the sandbox's
    keepalive/pause state and the background job registry to match.

    Returns:
        (status, output) where status is "RUNNING", "EXITED", or "UNKNOWN",
        or an error string (invalid job id / sandbox connect failure).
    """
    if not _JOB_ID_RE.match(job_id):
        return f"Error: invalid job id {job_id!r}."
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err

    log_path, pid_path = _paths(job_id)
    status_cmd = (
        f'if [ -f {pid_path} ]; then '
        f'pid=$(cat {pid_path}); '
        f'if kill -0 "$pid" 2>/dev/null; then echo RUNNING; else echo EXITED; fi; '
        f'else echo UNKNOWN; fi'
    )
    output_cmd = f"tail -n {max(1, tail_lines)} {log_path} 2>/dev/null"
    try:
        status_result = sandbox.commands.run(status_cmd, timeout=15)
        output_result = sandbox.commands.run(output_cmd, timeout=15)
    except Exception as e:
        _rearm_or_pause(sandbox, channel_id, thread_ts, still_running=False)
        return f"Error checking background command: {e}"

    status = (status_result.stdout or "").strip()
    output = output_result.stdout or "(no output yet)"

    if status != "RUNNING":
        from agent.background_jobs_store import unregister_job
        unregister_job(job_id)
    _rearm_or_pause(sandbox, channel_id, thread_ts, still_running=(status == "RUNNING"))
    return status, output


def check_background_command(channel_id: str, thread_ts: str, job_id: str, tail_lines: int = _DEFAULT_TAIL_LINES) -> str:
    """Report whether a background job is still running and show the tail of
    its output so far.

    Args:
        job_id: The id returned by run_background_command.
        tail_lines: How many of the most recent output lines to return.

    Returns:
        Status + recent output, or an error message.
    """
    result = get_job_status(channel_id, thread_ts, job_id, tail_lines)
    if isinstance(result, str):
        return result
    status, output = result
    if status == "UNKNOWN":
        return f"Error: no background command with id `{job_id}` found (or its sandbox was recreated since it started)."
    label = "still running" if status == "RUNNING" else "finished"
    return f"Job `{job_id}` is {label}. Last {tail_lines} line(s) of output:\n\n{output}"


def kill_background_command(channel_id: str, thread_ts: str, job_id: str) -> str:
    """Kill a running background job.

    Args:
        job_id: The id returned by run_background_command.

    Returns:
        Status message, or an error message.
    """
    if not _JOB_ID_RE.match(job_id):
        return f"Error: invalid job id {job_id!r}."
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err

    _, pid_path = _paths(job_id)
    cmd = (
        f'if [ -f {pid_path} ]; then '
        f'pid=$(cat {pid_path}); '
        f'kill "$pid" 2>/dev/null && echo KILLED || echo NOT_RUNNING; '
        f'else echo UNKNOWN; fi'
    )
    try:
        result = sandbox.commands.run(cmd, timeout=15)
    except Exception as e:
        _rearm_or_pause(sandbox, channel_id, thread_ts, still_running=False)
        return f"Error killing background command: {e}"

    from agent.background_jobs_store import unregister_job
    unregister_job(job_id)
    _rearm_or_pause(sandbox, channel_id, thread_ts, still_running=False)

    status = (result.stdout or "").strip()
    if status == "UNKNOWN":
        return f"Error: no background command with id `{job_id}` found."
    if status == "KILLED":
        return f"Killed job `{job_id}`."
    return f"Job `{job_id}` was not running (already finished)."
