"""Background process execution in the E2B sandbox — coolton's equivalent of
Claude Code's Bash(run_in_background) + BashOutput + KillShell, for a dev
server, watcher, or long build the model wants to start and keep checking on
instead of blocking a whole turn on run_linux_command.

Job state lives entirely in the sandbox's own filesystem (one log file + one
pid file per job, under ~/.coolton_bg/) rather than on the host — the sandbox
already persists across pause/resume for everything else, so a job started
in one turn is still there (running or finished) in a later turn, even
across a full app restart, with no separate host-side store to keep in sync.

A job is started with `setsid nohup ... &` on its own line (not combined with
`&&`/`;` on the same line as the `&` — that would background a compound list
and make `$!` capture a wrapping subshell's PID instead of the actual
detached process, which then couldn't be killed later) so it survives the
launching shell exiting and is fully detached from the sandbox's controlling
session.
"""

import re
import shlex
import uuid

from agent.sandbox_helpers import get_or_create_sandbox

_JOB_ID_RE = re.compile(r"^[a-f0-9]{8}$")
_BG_DIR = "~/.coolton_bg"
_DEFAULT_TAIL_LINES = 200


def _get_sandbox(channel_id: str, thread_ts: str):
    try:
        return get_or_create_sandbox(channel_id, thread_ts)[0], None
    except Exception as e:
        return None, f"Error: {e}"


def _paths(job_id: str) -> tuple[str, str]:
    return f"{_BG_DIR}/{job_id}.log", f"{_BG_DIR}/{job_id}.pid"


def run_background_command(channel_id: str, thread_ts: str, command: str, cwd: str = "") -> str:
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
        return f"Error starting background command: {e}"
    finally:
        sandbox.pause()

    if result.exit_code != 0:
        return f"Error starting background command (exit {result.exit_code}): {result.stderr}"
    return (
        f"Started background command with id `{job_id}`. Check its output with "
        f'check_background_command_tool(job_id="{job_id}"), stop it with '
        f'kill_background_command_tool(job_id="{job_id}").'
    )


def check_background_command(channel_id: str, thread_ts: str, job_id: str, tail_lines: int = _DEFAULT_TAIL_LINES) -> str:
    """Report whether a background job is still running and show the tail of
    its output so far.

    Args:
        job_id: The id returned by run_background_command.
        tail_lines: How many of the most recent output lines to return.

    Returns:
        Status + recent output, or an error message.
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
        return f"Error checking background command: {e}"
    finally:
        sandbox.pause()

    status = (status_result.stdout or "").strip()
    if status == "UNKNOWN":
        return f"Error: no background command with id `{job_id}` found (or its sandbox was recreated since it started)."
    label = "still running" if status == "RUNNING" else "finished"
    output = output_result.stdout or "(no output yet)"
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
        return f"Error killing background command: {e}"
    finally:
        sandbox.pause()

    status = (result.stdout or "").strip()
    if status == "UNKNOWN":
        return f"Error: no background command with id `{job_id}` found."
    if status == "KILLED":
        return f"Killed job `{job_id}`."
    return f"Job `{job_id}` was not running (already finished)."
