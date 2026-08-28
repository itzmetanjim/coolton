from types import SimpleNamespace

import pytest

from agent import desktop_helpers as dh


class _FakeHandle:
    def disconnect(self):
        pass


class _FakeCommands:
    """Records every command issued; xdpyinfo starts failing and flips to
    succeeding once an Xvfb command has been run, so ensure_desktop's
    start-then-poll-until-up flow terminates without a real sleep loop."""

    def __init__(self, display_already_up: bool):
        self.calls: list[tuple[str, dict | None]] = []
        self._xvfb_started = display_already_up

    def run(self, cmd, envs=None, timeout=None, background=None):
        self.calls.append((cmd, envs))
        if cmd.startswith("xdpyinfo"):
            return SimpleNamespace(exit_code=0 if self._xvfb_started else 1, stdout="", stderr="")
        if cmd.startswith("Xvfb"):
            self._xvfb_started = True
            return _FakeHandle()
        if cmd == "startxfce4":
            return _FakeHandle()
        return SimpleNamespace(exit_code=0, stdout="", stderr="")


class _FakeSandbox:
    def __init__(self, display_already_up: bool = True):
        self.commands = _FakeCommands(display_already_up)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(dh.time, "sleep", lambda s: None)


def test_ensure_desktop_starts_xvfb_and_xfce4_when_display_is_down():
    sandbox = _FakeSandbox(display_already_up=False)
    dh.ensure_desktop(sandbox, None)
    cmds = [c for c, _ in sandbox.commands.calls]
    assert any(c.startswith("Xvfb") for c in cmds)
    assert "startxfce4" in cmds


def test_ensure_desktop_is_a_noop_when_display_is_already_up():
    sandbox = _FakeSandbox(display_already_up=True)
    dh.ensure_desktop(sandbox, None)
    cmds = [c for c, _ in sandbox.commands.calls]
    assert not any(c.startswith("Xvfb") for c in cmds)
    assert "startxfce4" not in cmds
    # Only the single probe should have run.
    assert cmds == ["xdpyinfo -display :0"]


def test_press_key_combo_maps_to_exact_xdotool_command():
    sandbox = _FakeSandbox()
    dh.press_key(sandbox, None, ["ctrl", "c"])
    key_cmds = [c for c, _ in sandbox.commands.calls if c.startswith("xdotool key")]
    assert key_cmds == ["xdotool key Control_L+c"]


def test_type_text_shell_quotes_special_characters():
    import shlex

    text = 'rm -rf $HOME; echo "hi"'
    sandbox = _FakeSandbox()
    dh.type_text(sandbox, None, text, chunk_size=len(text))
    type_cmds = [c for c, _ in sandbox.commands.calls if c.startswith("xdotool type")]
    assert len(type_cmds) == 1
    # If $HOME/"/; were left unquoted, the sandbox shell would interpolate or
    # split them instead of passing the literal text to xdotool. shlex.split
    # applied to the full command reconstructs exactly the original text as a
    # single argument only if it was properly quoted.
    args = shlex.split(type_cmds[0])
    assert args[-1] == text


def test_every_desktop_command_carries_display_env():
    sandbox = _FakeSandbox(display_already_up=False)
    dh.press_key(sandbox, None, "enter")
    assert sandbox.commands.calls
    for _cmd, envs in sandbox.commands.calls:
        assert envs is not None
        assert envs.get("DISPLAY") == ":0"
