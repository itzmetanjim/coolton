from types import SimpleNamespace

import pytest

from agent import agent_browser_helpers as ab


class _FakeCommands:
    """Records every command issued. netstat reports the dashboard port as
    listening once the start command has run, so ensure_dashboard's
    start-then-poll-until-up flow terminates without a real sleep loop."""

    def __init__(self, dashboard_already_up: bool):
        self.calls: list[tuple[str, dict | None]] = []
        self._dashboard_up = dashboard_already_up

    def run(self, cmd, envs=None, timeout=None, background=None):
        self.calls.append((cmd, envs))
        if cmd.startswith("netstat"):
            up = self._dashboard_up
            return SimpleNamespace(
                exit_code=0, stdout=f"tcp 0 0 0.0.0.0:{ab.DASHBOARD_PORT}" if up else "", stderr="",
            )
        if cmd.startswith("agent-browser dashboard start"):
            self._dashboard_up = True
            return SimpleNamespace(exit_code=0, stdout="", stderr="")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")


class _FakeSandbox:
    def __init__(self, dashboard_already_up: bool = True):
        self.commands = _FakeCommands(dashboard_already_up)

    def get_host(self, port):
        return f"{port}-sbx-test.e2b.app"


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(ab.time, "sleep", lambda s: None)


def test_ensure_dashboard_starts_it_when_not_already_running():
    sandbox = _FakeSandbox(dashboard_already_up=False)
    ab.ensure_dashboard(sandbox, None)
    cmds = [c for c, _ in sandbox.commands.calls]
    assert any(c.startswith("agent-browser dashboard start") for c in cmds)


def test_ensure_dashboard_is_a_noop_when_already_running():
    sandbox = _FakeSandbox(dashboard_already_up=True)
    ab.ensure_dashboard(sandbox, None)
    cmds = [c for c, _ in sandbox.commands.calls]
    assert not any(c.startswith("agent-browser dashboard start") for c in cmds)
    # Only the single probe should have run.
    assert len(cmds) == 1


def test_ensure_dashboard_uses_the_configured_port():
    sandbox = _FakeSandbox(dashboard_already_up=False)
    ab.ensure_dashboard(sandbox, None)
    start_cmds = [c for c, _ in sandbox.commands.calls if c.startswith("agent-browser dashboard start")]
    assert start_cmds == [f"agent-browser dashboard start --port {ab.DASHBOARD_PORT}"]


def test_register_stream_registers_the_dashboard_host_and_returns_the_url(monkeypatch):
    sandbox = _FakeSandbox()
    posted = {}

    def fake_register(upstream_host):
        posted["upstream"] = upstream_host
        return "https://2390.proxy.tanjim.org/ab/tok123"

    monkeypatch.setattr("agent.web64_client.register_agent_browser_stream", fake_register)
    url = ab.register_stream(sandbox, None)
    assert posted["upstream"] == f"{ab.DASHBOARD_PORT}-sbx-test.e2b.app"
    assert url == "https://2390.proxy.tanjim.org/ab/tok123"
