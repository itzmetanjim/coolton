"""agent/tools/sandbox_background.py — coolton's equivalent of Claude Code's
Bash(run_in_background) + BashOutput + KillShell. Job OUTPUT lives entirely
in the sandbox's own filesystem (a log + pid file per job under
~/.coolton_bg/), so these tests fake sandbox.commands.run and assert on the
scripts it builds and how their canned output is interpreted. Job TRACKING
(agent.background_jobs_store, used to decide whether to keep the sandbox
warm instead of pausing — see this module's own docstring) is faked directly
so these tests don't touch the real background_jobs.json or spawn a real
sandbox_keepalive timer.
"""

from types import SimpleNamespace

import pytest

from agent.tools import sandbox_background as bg


class _FakeCommands:
    def __init__(self, canned=None):
        self.calls = []
        self._canned = canned or []  # list of (substring, SimpleNamespace(stdout=..., stderr=..., exit_code=...))
        self.default = SimpleNamespace(stdout="", stderr="", exit_code=0)

    def run(self, cmd, timeout=None):
        self.calls.append(cmd)
        for substring, response in self._canned:
            if substring in cmd:
                return response
        return self.default


class _FakeSandbox:
    def __init__(self, canned=None):
        self.commands = _FakeCommands(canned)
        self.paused = 0

    def pause(self):
        self.paused += 1


@pytest.fixture
def sandbox_env(monkeypatch):
    def _patch(canned=None):
        fake = _FakeSandbox(canned)
        monkeypatch.setattr(bg, "get_or_create_sandbox", lambda c, t: (fake, {}))
        return fake

    return _patch


@pytest.fixture(autouse=True)
def bg_store(monkeypatch):
    """Fakes agent.background_jobs_store (register/unregister/has_pending_jobs)
    and agent.sandbox_keepalive.arm — real versions would write to the repo's
    actual background_jobs.json and spawn a real threading.Timer. Defaults to
    "no other jobs pending" (has_pending_jobs -> False); override per test by
    reassigning has_pending_jobs on the returned namespace."""
    from agent import background_jobs_store, sandbox_keepalive

    state = SimpleNamespace(
        registered=[], unregistered=[], armed=[], has_pending=lambda c, t: False,
    )
    monkeypatch.setattr(background_jobs_store, "register_job", lambda *a: state.registered.append(a))
    monkeypatch.setattr(background_jobs_store, "unregister_job", lambda job_id: state.unregistered.append(job_id))
    monkeypatch.setattr(background_jobs_store, "has_pending_jobs", lambda c, t: state.has_pending(c, t))
    monkeypatch.setattr(sandbox_keepalive, "arm", lambda c, t, s: state.armed.append((c, t, s)))
    return state


# ---------------------------------------------------------------------------
# run_background_command
# ---------------------------------------------------------------------------


def test_run_background_command_returns_a_job_id(sandbox_env):
    sandbox_env()
    result = bg.run_background_command("C1", "1.1", "npm run dev", "U1")
    assert "Started background command with id `" in result


def test_run_background_command_backgrounds_on_its_own_line(sandbox_env):
    """setsid nohup ... & must be on its own statement, not combined with &&
    on the same line as the & — that backgrounds the whole compound list and
    makes $! capture a wrapping subshell's PID instead of the actual
    detached process, which then couldn't be found again to check/kill."""
    fake = sandbox_env()
    bg.run_background_command("C1", "1.1", "npm run dev", "U1")
    script = fake.commands.calls[0]
    lines = script.splitlines()
    bg_line = next(line for line in lines if line.rstrip().endswith("&"))
    assert "&&" not in bg_line
    assert bg_line.strip().startswith("setsid nohup")
    assert "npm run dev" in bg_line
    # $! is captured on the very next line, in the same shell invocation.
    idx = lines.index(bg_line)
    assert lines[idx + 1].strip().startswith("echo $!")


def test_run_background_command_includes_cwd_when_given(sandbox_env):
    fake = sandbox_env()
    bg.run_background_command("C1", "1.1", "npm run dev", "U1", cwd="/home/user/app")
    script = fake.commands.calls[0]
    assert "cd /home/user/app" in script


def test_run_background_command_reports_a_nonzero_exit(sandbox_env):
    fake = sandbox_env(canned=[("setsid", SimpleNamespace(stdout="", stderr="permission denied", exit_code=1))])
    result = bg.run_background_command("C1", "1.1", "npm run dev", "U1")
    assert "Error starting background command" in result
    assert "permission denied" in result
    assert fake.paused == 1


def test_run_background_command_propagates_sandbox_connect_errors(monkeypatch):
    def _boom(channel_id, thread_ts):
        raise RuntimeError("expired")

    monkeypatch.setattr(bg, "get_or_create_sandbox", _boom)
    assert bg.run_background_command("C1", "1.1", "npm run dev", "U1") == "Error: expired"


def test_run_background_command_registers_the_job(sandbox_env, bg_store):
    sandbox_env()
    bg.run_background_command("C1", "1.1", "npm run dev", "U1")
    assert len(bg_store.registered) == 1
    job_id, channel_id, thread_ts, user_id, command = bg_store.registered[0]
    assert (channel_id, thread_ts, user_id, command) == ("C1", "1.1", "U1", "npm run dev")
    assert len(job_id) == 8


def test_run_background_command_does_not_register_on_start_failure(sandbox_env, bg_store):
    sandbox_env(canned=[("setsid", SimpleNamespace(stdout="", stderr="nope", exit_code=1))])
    bg.run_background_command("C1", "1.1", "npm run dev", "U1")
    assert bg_store.registered == []


def test_run_background_command_keeps_sandbox_warm_instead_of_pausing(sandbox_env, bg_store):
    """A freshly-started job needs the sandbox to actually run, not freeze —
    pausing immediately (the old behavior) gave it milliseconds of CPU time."""
    fake = sandbox_env()
    bg.run_background_command("C1", "1.1", "npm run dev", "U1")
    assert fake.paused == 0
    assert bg_store.armed == [("C1", "1.1", bg.BG_JOB_KEEPALIVE_SECONDS)]


# ---------------------------------------------------------------------------
# check_background_command / get_job_status
# ---------------------------------------------------------------------------


def test_check_background_command_rejects_a_malformed_job_id(sandbox_env):
    sandbox_env()
    result = bg.check_background_command("C1", "1.1", "not-a-job-id")
    assert "invalid job id" in result


def test_check_background_command_still_running(sandbox_env):
    fake = sandbox_env(canned=[
        ("kill -0", SimpleNamespace(stdout="RUNNING\n", stderr="", exit_code=0)),
        ("tail -n", SimpleNamespace(stdout="server listening on :3000\n", stderr="", exit_code=0)),
    ])
    result = bg.check_background_command("C1", "1.1", "abcd1234")
    assert "is still running" in result
    assert "server listening on :3000" in result
    assert fake.paused == 0  # kept warm, not paused, while still running


def test_check_background_command_rearms_when_still_running(sandbox_env, bg_store):
    sandbox_env(canned=[("kill -0", SimpleNamespace(stdout="RUNNING\n", stderr="", exit_code=0))])
    bg.check_background_command("C1", "1.1", "abcd1234")
    assert bg_store.armed == [("C1", "1.1", bg.BG_JOB_KEEPALIVE_SECONDS)]
    assert bg_store.unregistered == []


def test_check_background_command_finished(sandbox_env):
    fake = sandbox_env(canned=[
        ("kill -0", SimpleNamespace(stdout="EXITED\n", stderr="", exit_code=0)),
        ("tail -n", SimpleNamespace(stdout="done\n", stderr="", exit_code=0)),
    ])
    result = bg.check_background_command("C1", "1.1", "abcd1234")
    assert "is finished" in result
    assert fake.paused == 1  # nothing left needs the sandbox warm


def test_check_background_command_unregisters_when_finished(sandbox_env, bg_store):
    sandbox_env(canned=[("kill -0", SimpleNamespace(stdout="EXITED\n", stderr="", exit_code=0))])
    bg.check_background_command("C1", "1.1", "abcd1234")
    assert bg_store.unregistered == ["abcd1234"]


def test_check_background_command_stays_warm_when_other_jobs_still_pending(sandbox_env, bg_store):
    """This job finished, but another job on the same thread hasn't — pausing
    now would freeze that other job's progress too."""
    fake = sandbox_env(canned=[("kill -0", SimpleNamespace(stdout="EXITED\n", stderr="", exit_code=0))])
    bg_store.has_pending = lambda c, t: True
    bg.check_background_command("C1", "1.1", "abcd1234")
    assert fake.paused == 0
    assert bg_store.armed == [("C1", "1.1", bg.BG_JOB_KEEPALIVE_SECONDS)]


def test_check_background_command_unknown_job(sandbox_env):
    result = sandbox_env(canned=[
        ("kill -0", SimpleNamespace(stdout="UNKNOWN\n", stderr="", exit_code=0)),
    ])
    result = bg.check_background_command("C1", "1.1", "abcd1234")
    assert "no background command with id" in result


def test_check_background_command_respects_tail_lines(sandbox_env):
    fake = sandbox_env(canned=[
        ("kill -0", SimpleNamespace(stdout="RUNNING\n", stderr="", exit_code=0)),
    ])
    bg.check_background_command("C1", "1.1", "abcd1234", tail_lines=50)
    assert any("tail -n 50" in c for c in fake.commands.calls)


def test_check_background_command_no_output_yet(sandbox_env):
    sandbox_env(canned=[
        ("kill -0", SimpleNamespace(stdout="RUNNING\n", stderr="", exit_code=0)),
        ("tail -n", SimpleNamespace(stdout="", stderr="", exit_code=1)),
    ])
    result = bg.check_background_command("C1", "1.1", "abcd1234")
    assert "(no output yet)" in result


def test_get_job_status_returns_status_and_output_tuple(sandbox_env):
    sandbox_env(canned=[
        ("kill -0", SimpleNamespace(stdout="RUNNING\n", stderr="", exit_code=0)),
        ("tail -n", SimpleNamespace(stdout="hi\n", stderr="", exit_code=0)),
    ])
    result = bg.get_job_status("C1", "1.1", "abcd1234")
    assert result == ("RUNNING", "hi\n")


def test_get_job_status_invalid_job_id_is_an_error_string(sandbox_env):
    sandbox_env()
    result = bg.get_job_status("C1", "1.1", "nope!")
    assert isinstance(result, str)
    assert "invalid job id" in result


# ---------------------------------------------------------------------------
# kill_background_command
# ---------------------------------------------------------------------------


def test_kill_background_command_rejects_a_malformed_job_id(sandbox_env):
    sandbox_env()
    result = bg.kill_background_command("C1", "1.1", "nope!")
    assert "invalid job id" in result


def test_kill_background_command_success(sandbox_env):
    fake = sandbox_env(canned=[("kill \"$pid\"", SimpleNamespace(stdout="KILLED\n", stderr="", exit_code=0))])
    result = bg.kill_background_command("C1", "1.1", "abcd1234")
    assert "Killed job `abcd1234`" in result
    assert fake.paused == 1


def test_kill_background_command_unregisters_the_job(sandbox_env, bg_store):
    sandbox_env(canned=[("kill \"$pid\"", SimpleNamespace(stdout="KILLED\n", stderr="", exit_code=0))])
    bg.kill_background_command("C1", "1.1", "abcd1234")
    assert bg_store.unregistered == ["abcd1234"]


def test_kill_background_command_already_finished(sandbox_env):
    sandbox_env(canned=[("kill \"$pid\"", SimpleNamespace(stdout="NOT_RUNNING\n", stderr="", exit_code=0))])
    result = bg.kill_background_command("C1", "1.1", "abcd1234")
    assert "was not running" in result


def test_kill_background_command_unknown_job(sandbox_env):
    sandbox_env(canned=[("kill \"$pid\"", SimpleNamespace(stdout="UNKNOWN\n", stderr="", exit_code=0))])
    result = bg.kill_background_command("C1", "1.1", "abcd1234")
    assert "no background command with id" in result
