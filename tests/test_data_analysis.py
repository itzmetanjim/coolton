import json
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
    assert data_analysis.run_opencode_in_sandbox("C1", "1.1", task) == "done"
    assert shlex.split(calls[-1])[:3] == ["printf", "%s", task]


def test_opencode_only_runs_on_models_that_dont_train(monkeypatch):
    calls = []

    class _Commands:
        def run(self, cmd, envs=None, timeout=None):
            calls.append(cmd)
            return SimpleNamespace(stdout="done", stderr="", exit_code=0)

    monkeypatch.setattr(data_analysis, "get_or_create_sandbox", lambda c, t: (SimpleNamespace(commands=_Commands()), {}))
    assert data_analysis.run_opencode_in_sandbox("C1", "1.1", "task", model="opencode/some-other-model").startswith("Error")
    assert not calls

    data_analysis.run_opencode_in_sandbox("C1", "1.1", "task")
    args = shlex.split(calls[-1].split("|", 1)[1])
    default = data_analysis.OPENCODE_MODELS[0]
    assert args[args.index("--model") + 1] == default
    config = next(a for a in args if a.startswith("OPENCODE_CONFIG_CONTENT=")).split("=", 1)[1]
    assert json.loads(config) == {"model": default, "small_model": default}
