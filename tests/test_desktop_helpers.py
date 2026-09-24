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


# ---------------------------------------------------------------------------
# start_stream — the shared link must be view-only at the SERVER, not just via
# noVNC's client-side view_only=true URL param.
# ---------------------------------------------------------------------------


class _StreamCommands(_FakeCommands):
    def __init__(self, viewonly_running=False, novnc_listening=False):
        super().__init__(display_already_up=True)
        self.viewonly_running = viewonly_running
        self.novnc_listening = novnc_listening

    def run(self, cmd, envs=None, timeout=None, background=None):
        if not cmd.startswith(("pgrep", "cat ", "netstat")) and "novnc_proxy" not in cmd:
            return super().run(cmd, envs=envs, timeout=timeout, background=background)
        self.calls.append((cmd, envs))
        if cmd.startswith("pgrep"):
            if not self.viewonly_running:
                raise RuntimeError("exit 1")
            return SimpleNamespace(exit_code=0, stdout="123", stderr="")
        if cmd.startswith("cat "):
            return SimpleNamespace(exit_code=0, stdout="existingpass", stderr="")
        if cmd.startswith("netstat"):
            return SimpleNamespace(exit_code=0, stdout="tcp 6080" if self.novnc_listening else "", stderr="")
        self.novnc_listening = True  # novnc_proxy
        return _FakeHandle()


def _stream_sandbox(**kwargs):
    sandbox = _FakeSandbox()
    sandbox.commands = _StreamCommands(**kwargs)
    sandbox.get_host = lambda port: f"{port}-sbx.e2b.app"
    return sandbox


def test_start_stream_runs_x11vnc_view_only():
    sandbox = _stream_sandbox()
    url = dh.start_stream(sandbox, None)
    x11vnc = [c for c, _ in sandbox.commands.calls if c.startswith("x11vnc -bg")]
    assert len(x11vnc) == 1 and "-viewonly" in x11vnc[0]
    assert "view_only=true" in url


def test_start_stream_replaces_a_non_view_only_server_but_reuses_novnc():
    """A sandbox resumed from before -viewonly was enforced still has a
    full-control x11vnc (and noVNC) running: replace the former, keep the latter."""
    sandbox = _stream_sandbox(viewonly_running=False, novnc_listening=True)
    dh.start_stream(sandbox, None)
    cmds = [c for c, _ in sandbox.commands.calls]
    assert "pkill -x x11vnc || true" in cmds
    assert cmds.index("pkill -x x11vnc || true") < next(i for i, c in enumerate(cmds) if c.startswith("x11vnc -bg"))
    assert not any("novnc_proxy" in c for c in cmds)


def test_start_stream_reuses_a_running_view_only_server():
    sandbox = _stream_sandbox(viewonly_running=True, novnc_listening=True)
    url = dh.start_stream(sandbox, None)
    assert "password=existingpass" in url
    assert not any(c.startswith("x11vnc") for c, _ in sandbox.commands.calls)
