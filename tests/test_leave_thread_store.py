import pytest

from agent import leave_thread_store as store


@pytest.fixture
def tmp_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "LEAVE_THREAD_STORE_FILE", str(tmp_path / "leave_thread_store.json"))
    return tmp_path


def test_missing_entry_defaults_to_dm(tmp_store):
    assert store.is_thread_engaged("C1", "1.1", is_dm=True) is True
    assert store.is_thread_engaged("C1", "1.1", is_dm=False) is False


def test_join_then_engaged(tmp_store):
    store.join_thread("C1", "1.1")
    assert store.is_thread_engaged("C1", "1.1", is_dm=False) is True


def test_leave_then_not_engaged(tmp_store):
    store.join_thread("C1", "1.1")
    store.leave_thread("C1", "1.1")
    assert store.is_thread_engaged("C1", "1.1", is_dm=False) is False


def test_threads_are_independent(tmp_store):
    store.join_thread("C1", "1.1")
    store.leave_thread("C2", "2.2")
    assert store.is_thread_engaged("C1", "1.1") is True
    assert store.is_thread_engaged("C2", "2.2") is False


def test_return_messages(tmp_store):
    assert store.join_thread("C1", "1.1").startswith("Joined thread")
    assert store.leave_thread("C1", "1.1").startswith("Left thread")


def test_corrupt_file_falls_back_to_default(tmp_store):
    (tmp_store / "leave_thread_store.json").write_text("{nope")
    assert store.is_thread_engaged("C1", "1.1", is_dm=True) is True
    assert store.is_thread_engaged("C1", "1.1", is_dm=False) is False


def test_persists_to_disk(tmp_store):
    store.join_thread("C1", "1.1")
    data = __import__("json").loads((tmp_store / "leave_thread_store.json").read_text())
    assert data["C1:1.1"]["engaged"] is True
