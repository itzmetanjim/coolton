"""agent/tools/sandbox_background.py — coolton's equivalent of Claude Code's
Bash(run_in_background) + BashOutput + KillShell. Job state lives entirely in
the sandbox's own filesystem (a log + pid file per job under ~/.coolton_bg/),
so these tests fake sandbox.commands.run and assert on the scripts it builds
and how their canned output is interpreted.
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


# ---------------------------------------------------------------------------
# run_background_command
# ---------------------------------------------------------------------------


def test_run_background_command_returns_a_job_id(sandbox_env):
    fake = sandbox_env()
    result = bg.run_background_command("C1", "1.1", "npm run dev")
    assert "Started background command with id `" in result
    assert fake.paused == 1


def test_run_background_command_backgrounds_on_its_own_line(sandbox_env):
    """setsid nohup ... & must be on its own statement, not combined with &&
    on the same line as the & — that backgrounds the whole compound list and
    makes $! capture a wrapping subshell's PID instead of the actual
    detached process, which then couldn't be found again to check/kill."""
    fake = sandbox_env()
    bg.run_background_command("C1", "1.1", "npm run dev")
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
    bg.run_background_command("C1", "1.1", "npm run dev", cwd="/home/user/app")
    script = fake.commands.calls[0]
    assert "cd /home/user/app" in script


def test_run_background_command_reports_a_nonzero_exit(sandbox_env):
    sandbox_env(canned=[("setsid", SimpleNamespace(stdout="", stderr="permission denied", exit_code=1))])
    result = bg.run_background_command("C1", "1.1", "npm run dev")
    assert "Error starting background command" in result
    assert "permission denied" in result


def test_run_background_command_propagates_sandbox_connect_errors(monkeypatch):
    def _boom(channel_id, thread_ts):
        raise RuntimeError("expired")

    monkeypatch.setattr(bg, "get_or_create_sandbox", _boom)
    assert bg.run_background_command("C1", "1.1", "npm run dev") == "Error: expired"


# ---------------------------------------------------------------------------
# check_background_command
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
    assert fake.paused == 1


def test_check_background_command_finished(sandbox_env):
    sandbox_env(canned=[
        ("kill -0", SimpleNamespace(stdout="EXITED\n", stderr="", exit_code=0)),
        ("tail -n", SimpleNamespace(stdout="done\n", stderr="", exit_code=0)),
    ])
    result = bg.check_background_command("C1", "1.1", "abcd1234")
    assert "is finished" in result


def test_check_background_command_unknown_job(sandbox_env):
    sandbox_env(canned=[
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


def test_kill_background_command_already_finished(sandbox_env):
    sandbox_env(canned=[("kill \"$pid\"", SimpleNamespace(stdout="NOT_RUNNING\n", stderr="", exit_code=0))])
    result = bg.kill_background_command("C1", "1.1", "abcd1234")
    assert "was not running" in result


def test_kill_background_command_unknown_job(sandbox_env):
    sandbox_env(canned=[("kill \"$pid\"", SimpleNamespace(stdout="UNKNOWN\n", stderr="", exit_code=0))])
    result = bg.kill_background_command("C1", "1.1", "abcd1234")
    assert "no background command with id" in result
