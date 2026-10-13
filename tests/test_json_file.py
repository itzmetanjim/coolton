"""agent.json_file: how coolton's state files are read and written."""
import os

from agent.json_file import load_json, save_json


def test_round_trip_and_a_missing_file_is_the_default(tmp_path):
    path = tmp_path / "store.json"
    assert load_json(path) == {}
    assert load_json(path, default=lambda: {"jobs": []}) == {"jobs": []}
    save_json(path, {"a": 1})
    assert load_json(path) == {"a": 1}


def test_a_corrupt_file_is_kept_aside_not_overwritten(tmp_path):
    """Treating it as empty used to mean the next save replaced it: a corrupt ban list
    or schedule file lost every entry for good."""
    path = tmp_path / "bans.json"
    path.write_text('{"U1": tru')
    assert load_json(path) == {}
    (aside,) = [p for p in tmp_path.iterdir() if p.name.startswith("bans.json.corrupt-")]
    assert aside.read_text() == '{"U1": tru'

    path.write_text("[1, 2]")  # valid JSON, wrong kind
    assert load_json(path) == {}
    assert len([p for p in tmp_path.iterdir() if ".corrupt-" in p.name]) >= 1


def test_private_files_are_owner_only_and_no_temp_file_is_left(tmp_path):
    path = tmp_path / "secrets.json"
    save_json(path, {"token": "x"}, private=True)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert [p.name for p in tmp_path.iterdir()] == ["secrets.json"]


def test_a_file_that_cant_be_read_is_left_where_it_is(tmp_path):
    """A permission problem isn't corruption: setting the file aside would have
    started a conversation's memory over (seen live, when a run as root left
    conversations/ owned by root)."""
    import pytest

    if os.geteuid() == 0:
        pytest.skip("root can read any file")
    path = tmp_path / "C1%3A1.1.json"
    path.write_text('{"messages": []}')
    path.chmod(0)
    try:
        assert load_json(path) == {}
        assert [p.name for p in tmp_path.iterdir()] == ["C1%3A1.1.json"]
    finally:
        path.chmod(0o600)
