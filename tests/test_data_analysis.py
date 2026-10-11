import shlex
from types import SimpleNamespace

from agent.tools import data_analysis


def test_opencode_gets_the_task_whole_however_its_quoted(monkeypatch):
    """The task used to sit inside single quotes, so an apostrophe ended it early
    and the rest ran as shell."""
    calls = []

    class _Commands:
        def run(self, cmd, envs=None, timeout=None):
            calls.append(cmd)
            return SimpleNamespace(stdout="/usr/bin/opencode" if cmd.startswith("which") else "done", stderr="", exit_code=0)

    monkeypatch.setattr(data_analysis, "get_or_create_sandbox", lambda c, t: (SimpleNamespace(commands=_Commands()), {}))
    task = "fix the user's bug'; touch /tmp/pwned; echo '"
    assert data_analysis.run_opencode_in_sandbox("C1", "1.1", task, model="a b") == "done"
    assert shlex.split(calls[-1])[:3] == ["printf", "%s", task]
    assert "--model 'a b'" in calls[-1]
