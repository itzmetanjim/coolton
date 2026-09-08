import pytest

from agent import background_jobs_store as store


@pytest.fixture
def tmp_file(monkeypatch, tmp_path):
    path = str(tmp_path / "background_jobs.json")
    monkeypatch.setattr(store, "BACKGROUND_JOBS_FILE", path)
    return path


def test_register_job_saves_all_fields(tmp_file):
    store.register_job("abcd1234", "C1", "1.1", "U1", "npm run dev")
    jobs = store.list_jobs()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_id"] == "abcd1234"
    assert job["channel_id"] == "C1"
    assert job["thread_ts"] == "1.1"
    assert job["user_id"] == "U1"
    assert job["command"] == "npm run dev"
    assert "started_at" in job


def test_unregister_job_removes_only_that_job(tmp_file):
    store.register_job("job1", "C1", "1.1", "U1", "cmd1")
    store.register_job("job2", "C1", "1.1", "U1", "cmd2")
    store.unregister_job("job1")
    jobs = store.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "job2"


def test_unregister_job_missing_is_a_noop(tmp_file):
    store.register_job("job1", "C1", "1.1", "U1", "cmd1")
    store.unregister_job("nonexistent")
    assert len(store.list_jobs()) == 1


def test_has_pending_jobs_true_for_matching_thread(tmp_file):
    store.register_job("job1", "C1", "1.1", "U1", "cmd1")
    assert store.has_pending_jobs("C1", "1.1") is True


def test_has_pending_jobs_false_for_different_thread(tmp_file):
    store.register_job("job1", "C1", "1.1", "U1", "cmd1")
    assert store.has_pending_jobs("C1", "2.2") is False
    assert store.has_pending_jobs("C2", "1.1") is False


def test_has_pending_jobs_false_after_unregister(tmp_file):
    store.register_job("job1", "C1", "1.1", "U1", "cmd1")
    store.unregister_job("job1")
    assert store.has_pending_jobs("C1", "1.1") is False


def test_load_missing_or_corrupt_file(tmp_file):
    assert store.list_jobs() == []
    with open(tmp_file, "w") as f:
        f.write("{broken")
    assert store.list_jobs() == []


def test_load_rejects_non_dict_or_missing_jobs_key(tmp_file):
    with open(tmp_file, "w") as f:
        f.write("[]")
    assert store.list_jobs() == []
    with open(tmp_file, "w") as f:
        f.write('{"jobs": "not a list"}')
    assert store.list_jobs() == []
