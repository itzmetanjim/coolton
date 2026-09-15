"""Shared fixtures.

`isolated_config` swaps agent.provider_config's module-level cache for a
throwaway providers.json for the duration of a test, then resets it. Tests
for provider-selection *logic* (tag filtering, BYOK ordering, vision
matching, env-var wiring, ...) should build a small synthetic config with
this rather than asserting against the real providers.json's actual
content — the real file changes routinely (models get added/removed/
renamed as providers go down or better options show up), and a test that
hardcodes today's specific tags/model ids just breaks in lockstep with
every such edit without having caught anything the edit itself didn't
already need eyeballing. A test that instead makes up its own "p1"/"m1"
fixture data keeps testing the exact same logic and never needs to change
for that reason again.

The one exception is a genuine data-completeness/structural check on the
real config itself (e.g. "every model declares a context_window") — those
intentionally read the live providers.json and should keep doing so.
"""

import json

import pytest

from agent import provider_config
from agent import stopped_threads_store


@pytest.fixture(autouse=True)
def clean_stopped_threads_store():
    """RFC i rule 2 tests mark threads stopped in the real in-process store;
    clear it around every test so marked threads can't leak across tests."""
    stopped_threads_store._stopped_threads.clear()
    yield
    stopped_threads_store._stopped_threads.clear()


@pytest.fixture(autouse=True)
def isolated_file_stores(monkeypatch, tmp_path):
    """File-backed stores must never be read from or written to the working
    directory during tests. Handler tests call real join_thread()/is_banned()/
    is_code_channel() — without this, one test's join persists to the real
    gitignored store file and silently changes what every LATER test run
    (and every other test) sees. Point each store at a throwaway file."""
    from agent import ban_store, code_channel_store, leave_thread_store

    monkeypatch.setattr(leave_thread_store, "LEAVE_THREAD_STORE_FILE", str(tmp_path / "leave_thread_store.json"))
    monkeypatch.setattr(ban_store, "BAN_STORE_FILE", str(tmp_path / "ban_store.json"))
    monkeypatch.setattr(code_channel_store, "CODE_CHANNEL_STORE_FILE", str(tmp_path / "code_channels.json"))


@pytest.fixture
def isolated_config(tmp_path):
    def _write(data: dict):
        path = tmp_path / "providers.json"
        path.write_text(json.dumps(data))
        provider_config._load_config(str(path))
        return path

    yield _write
    provider_config._reset()
