import pytest

from agent import sandbox_store as store


@pytest.fixture
def tmp_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "SANDBOX_STORE_FILE", str(tmp_path / "thread_sandboxes.json"))
    return tmp_path


def test_missing_file_returns_none(tmp_store):
    assert store.get_thread_sandbox_id("C1", "1.1") is None


def test_save_and_get_roundtrip(tmp_store):
    store.save_thread_sandbox_id("C1", "1.1", "sandbox-abc")
    assert store.get_thread_sandbox_id("C1", "1.1") == "sandbox-abc"


def test_threads_independent(tmp_store):
    store.save_thread_sandbox_id("C1", "1.1", "sandbox-a")
    store.save_thread_sandbox_id("C2", "2.2", "sandbox-b")
    assert store.get_thread_sandbox_id("C1", "1.1") == "sandbox-a"
    assert store.get_thread_sandbox_id("C2", "2.2") == "sandbox-b"
    assert store.get_thread_sandbox_id("C1", "9.9") is None


def test_corrupt_file_returns_none(tmp_store):
    (tmp_store / "thread_sandboxes.json").write_text("{broken")
    assert store.get_thread_sandbox_id("C1", "1.1") is None


def test_save_overwrites_corrupt_file(tmp_store):
    (tmp_store / "thread_sandboxes.json").write_text("{broken")
    store.save_thread_sandbox_id("C1", "1.1", "sandbox-new")
    assert store.get_thread_sandbox_id("C1", "1.1") == "sandbox-new"


def test_overwrite_same_thread(tmp_store):
    store.save_thread_sandbox_id("C1", "1.1", "sandbox-old")
    store.save_thread_sandbox_id("C1", "1.1", "sandbox-new")
    assert store.get_thread_sandbox_id("C1", "1.1") == "sandbox-new"
