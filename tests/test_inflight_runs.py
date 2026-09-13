import json

import pytest

from agent import inflight_runs


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(inflight_runs, "STORE_PATH", tmp_path / "inflight_runs.json")


def test_record_start_then_pop_all_returns_it():
    inflight_runs.record_start("C1", "1.1", message_ts="111.111", user_id="U1", text="hi", is_slack=True)
    entries = inflight_runs.pop_all()
    assert entries == [
        {"channel_id": "C1", "thread_ts": "1.1", "message_ts": "111.111", "user_id": "U1", "text": "hi", "is_slack": True},
    ]


def test_pop_all_clears_the_store():
    inflight_runs.record_start("C1", "1.1", message_ts="111.111", user_id="U1", text="hi", is_slack=True)
    inflight_runs.pop_all()
    assert inflight_runs.pop_all() == []


def test_record_finished_removes_the_entry():
    inflight_runs.record_start("C1", "1.1", message_ts="111.111", user_id="U1", text="hi", is_slack=True)
    inflight_runs.record_finished("C1", "1.1")
    assert inflight_runs.pop_all() == []


def test_record_finished_is_a_noop_for_an_unknown_entry():
    inflight_runs.record_finished("nope", "nope")
    assert inflight_runs.pop_all() == []


def test_a_normal_start_then_finish_leaves_nothing_orphaned():
    """The happy path: a run that completes normally clears its own record —
    resume_orphaned_runs should never see it."""
    inflight_runs.record_start("web", "conv1", message_ts="5", user_id="U1", text="hi", is_slack=False)
    inflight_runs.record_finished("web", "conv1")
    assert inflight_runs.pop_all() == []


def test_multiple_entries_are_tracked_independently():
    inflight_runs.record_start("C1", "1.1", message_ts="1", user_id="U1", text="a", is_slack=True)
    inflight_runs.record_start("web", "conv1", message_ts="2", user_id="U2", text="b", is_slack=False)
    inflight_runs.record_finished("C1", "1.1")
    entries = inflight_runs.pop_all()
    assert len(entries) == 1
    assert entries[0]["channel_id"] == "web"


def test_corrupt_json_is_tolerated(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.json"
    path.write_text("not json")
    monkeypatch.setattr(inflight_runs, "STORE_PATH", path)
    assert inflight_runs.pop_all() == []


def test_non_dict_json_is_tolerated(tmp_path, monkeypatch):
    path = tmp_path / "nondict.json"
    path.write_text(json.dumps([1, 2, 3]))
    monkeypatch.setattr(inflight_runs, "STORE_PATH", path)
    assert inflight_runs.pop_all() == []


def test_missing_file_is_tolerated(tmp_path, monkeypatch):
    monkeypatch.setattr(inflight_runs, "STORE_PATH", tmp_path / "does-not-exist.json")
    assert inflight_runs.pop_all() == []
