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

`_isolated_leave_thread_store` (autouse) points agent.leave_thread_store at a
per-test temp file. Handler tests (message/app_mention, policy opt-in) join
threads as a side effect; without this they wrote leave_thread_store.json
into the working directory, and the "joined" threads it recorded leaked into
later runs (e.g. making a thread look engaged that a test expects not to be).
"""

import json

import pytest

from agent import leave_thread_store, provider_config


@pytest.fixture(autouse=True)
def _isolated_leave_thread_store(tmp_path, monkeypatch):
    monkeypatch.setattr(leave_thread_store, "LEAVE_THREAD_STORE_FILE", str(tmp_path / "leave_thread_store.json"))


@pytest.fixture
def isolated_config(tmp_path):
    def _write(data: dict):
        path = tmp_path / "providers.json"
        path.write_text(json.dumps(data))
        provider_config._load_config(str(path))
        return path

    yield _write
    provider_config._reset()
