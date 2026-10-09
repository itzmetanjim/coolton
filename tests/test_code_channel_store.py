import pytest

from agent import code_channel_store as store


@pytest.fixture
def tmp_file(monkeypatch, tmp_path):
    path = str(tmp_path / "code_channels.json")
    monkeypatch.setattr(store, "CODE_CHANNEL_STORE_FILE", path)
    return path


def test_register_and_is_code_channel(tmp_file):
    assert store.is_code_channel("C1") is False
    store.register_code_channel("C1", "My Code Channel", "U1", "C0", "1.1")
    assert store.is_code_channel("C1") is True


def test_get_code_channel_returns_all_fields(tmp_file):
    store.register_code_channel("C1", "Code audit and bug detection", "U1", "C0", "1.1")
    entry = store.get_code_channel("C1")
    assert entry["name"] == "Code audit and bug detection"
    assert entry["owner_id"] == "U1"
    assert entry["source_channel_id"] == "C0"
    assert entry["source_thread_ts"] == "1.1"
    assert "created_at" in entry


def test_get_code_channel_missing_returns_none(tmp_file):
    assert store.get_code_channel("nonexistent") is None


def test_is_code_channel_false_for_missing_file(tmp_file):
    assert store.is_code_channel("C1") is False


def test_load_tolerates_corrupt_json(tmp_file):
    with open(tmp_file, "w") as f:
        f.write("{not valid json")
    assert store.is_code_channel("C1") is False


def test_load_tolerates_non_dict_json(tmp_file):
    with open(tmp_file, "w") as f:
        f.write("[1, 2, 3]")
    assert store.is_code_channel("C1") is False


def test_register_multiple_channels_independent(tmp_file):
    store.register_code_channel("C1", "First", "U1", "C0", "1.1")
    store.register_code_channel("C2", "Second", "U2", "C0", "2.2")
    assert store.is_code_channel("C1") is True
    assert store.is_code_channel("C2") is True
    assert store.get_code_channel("C1")["name"] == "First"
    assert store.get_code_channel("C2")["name"] == "Second"


def test_only_the_context_opener_of_a_channel_coolton_made_is_recognized():
    """Slack opens a code channel with "Context from #origin" quoting the request; coolton's
    handoff covers it when she made the channel, while one made from Slack's UI starts from it."""
    opener = ("<https://hackclub.slack.com/archives/C0BSF4XM3KJ/p1791549628540459|Context> from "
              "<#C0BSF4XM3KJ>:\n> <@UCOOLTON> make a code channel")
    assert not store.is_expected_origin_context(opener)
    store.expect_origin_context("C0BSF4XM3KJ", "1791549628.540459")
    assert store.is_expected_origin_context(opener)
    assert not store.is_expected_origin_context("> " + opener)
