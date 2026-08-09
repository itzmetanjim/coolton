from types import SimpleNamespace

import pytest

import agent.sandbox_helpers as helpers_mod


class _FakeCommands:
    def __init__(self, exc=None):
        self.exc = exc

    def run(self, cmd, envs=None, timeout=None):
        if self.exc:
            raise self.exc
        return SimpleNamespace(stdout="", stderr="", exit_code=0)


class _FakeSandbox:
    def __init__(self, sandbox_id="sbx-new", exc=None):
        self.sandbox_id = sandbox_id
        self.commands = _FakeCommands(exc)
        self.killed = False

    def kill(self):
        self.killed = True


class _FakeSandboxAPI:
    def __init__(self, existing=None):
        self.existing = existing or {}
        self.created = []

    def connect(self, sandbox_id):
        if sandbox_id in self.existing:
            return self.existing[sandbox_id]
        return _FakeSandbox(sandbox_id)

    def create(self, template_id=None):
        sb = _FakeSandbox(f"sbx-{len(self.created) + 1}")
        self.created.append(sb)
        return sb


_STALE = ("<StreamReset stream_id:1, error_code:2, remote_reset:True>: "
          "The sandbox was killed or reached its end of life while the request was in flight")


@pytest.fixture
def sandbox_env(monkeypatch):
    tokens = []

    def _patch(store_id=None, api=None):
        monkeypatch.setattr(helpers_mod, "Sandbox", api or _FakeSandboxAPI())
        monkeypatch.setattr(helpers_mod, "get_thread_sandbox_id", lambda c, t: store_id)
        monkeypatch.setattr(helpers_mod, "issue_sandbox_token", lambda sid: f"tok-{sid}")
        monkeypatch.setattr(helpers_mod, "save_thread_sandbox_id", lambda c, t, sid: tokens.append(("save", c, t, sid)))
        monkeypatch.setattr(helpers_mod, "delete_thread_sandbox_id", lambda c, t: tokens.append(("del", c, t)))
        monkeypatch.setattr(helpers_mod, "_provision_sandbox", lambda sb, pi=None: "provisioned")
        return tokens

    return _patch


# ---------------------------------------------------------------------------
# _is_stale_sandbox_error
# ---------------------------------------------------------------------------


def test_stale_error_matches_streamreset():
    assert helpers_mod._is_stale_sandbox_error(Exception(_STALE))


def test_stale_error_matches_other_markers():
    assert helpers_mod._is_stale_sandbox_error(Exception("Sandbox is not running"))
    assert helpers_mod._is_stale_sandbox_error(Exception("Paused sandbox sbx-1 not found"))
    from e2b.exceptions import SandboxNotFoundException

    assert helpers_mod._is_stale_sandbox_error(SandboxNotFoundException("not found"))


def test_stale_error_does_not_match_ordinary_errors():
    assert not helpers_mod._is_stale_sandbox_error(Exception("command failed: no space left on device"))
    assert not helpers_mod._is_stale_sandbox_error(Exception("pip install failed"))


# ---------------------------------------------------------------------------
# get_or_create_sandbox
# ---------------------------------------------------------------------------


def test_creates_and_provisions_when_none_stored(sandbox_env):
    api = _FakeSandboxAPI()
    tokens = sandbox_env(store_id=None, api=api)
    sb, proxy = helpers_mod.get_or_create_sandbox("C1", "1.1")
    assert sb is api.created[0]
    assert proxy["token"] == f"tok-{sb.sandbox_id}"
    assert tokens == [("save", "C1", "1.1", sb.sandbox_id)]


def test_reuses_live_stored_sandbox(sandbox_env):
    live = _FakeSandbox("sbx-live")
    api = _FakeSandboxAPI(existing={"sbx-live": live})
    tokens = sandbox_env(store_id="sbx-live", api=api)
    sb, proxy = helpers_mod.get_or_create_sandbox("C1", "1.1")
    assert sb is live
    assert proxy["token"] == "tok-sbx-live"
    assert tokens == []
    assert api.created == []


def test_recreates_when_stored_sandbox_is_dead(sandbox_env):
    dead = _FakeSandbox("sbx-dead", exc=RuntimeError(_STALE))
    api = _FakeSandboxAPI(existing={"sbx-dead": dead})
    tokens = sandbox_env(store_id="sbx-dead", api=api)
    sb, proxy = helpers_mod.get_or_create_sandbox("C1", "1.1")
    assert dead.killed is True
    assert tokens[0] == ("del", "C1", "1.1")
    assert sb is api.created[0]
    assert tokens[-1] == ("save", "C1", "1.1", sb.sandbox_id)


def test_non_stale_error_propagates(sandbox_env):
    bad = _FakeSandbox("sbx-bad", exc=RuntimeError("command failed: no space left on device"))
    api = _FakeSandboxAPI(existing={"sbx-bad": bad})
    sandbox_env(store_id="sbx-bad", api=api)
    with pytest.raises(RuntimeError, match="no space"):
        helpers_mod.get_or_create_sandbox("C1", "1.1")
    assert api.created == []
    assert bad.killed is False
