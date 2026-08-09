from types import SimpleNamespace

import pytest

from agent import fallback_cache as fc


@pytest.fixture
def clock(monkeypatch, tmp_path):
    """Replace fallback_cache.time with a controllable clock + tmp store file."""
    now = [1000.0]
    monkeypatch.setattr(fc, "time", SimpleNamespace(time=lambda: now[0]))
    monkeypatch.setattr(fc, "FALLBACK_CACHE_FILE", str(tmp_path / "fallback_cache.json"))

    def advance(seconds: float):
        now[0] += seconds

    return advance


def test_empty_initial_state(clock):
    assert fc.get_working_provider() is None
    assert fc.get_dead_providers() == {}


def test_set_and_get_working_provider(clock):
    fc.set_working_provider("anthropic")
    assert fc.get_working_provider() == "anthropic"


def test_working_provider_ttl_expires(clock):
    fc.set_working_provider("anthropic")
    clock(fc.WORKING_TTL_SECONDS + 1)
    assert fc.get_working_provider() is None


def test_mark_dead_and_reason(clock):
    fc.mark_dead("groq_oss120b", "401 unauthorized")
    assert fc.get_dead_providers() == {"groq_oss120b": "401 unauthorized"}


def test_dead_ttl_expires(clock):
    fc.mark_dead("openai", "rate limited")
    clock(fc.DEAD_TTL_SECONDS + 1)
    assert fc.get_dead_providers() == {}


def test_mark_dead_clears_working_for_same_provider(clock):
    fc.set_working_provider("openai")
    fc.mark_dead("openai", "boom")
    assert fc.get_working_provider() is None


def test_set_working_clears_dead_mark(clock):
    fc.mark_dead("openai", "boom")
    fc.set_working_provider("openai")
    assert fc.get_dead_providers() == {}
    assert fc.get_working_provider() == "openai"


def test_dead_reason_truncated(clock):
    fc.mark_dead("openai", "x" * 500)
    assert len(fc.get_dead_providers()["openai"]) == 300


def test_clear_cache(clock):
    fc.set_working_provider("openai")
    fc.mark_dead("groq_qwen27b", "err")
    fc.clear_cache()
    assert fc.get_working_provider() is None
    assert fc.get_dead_providers() == {}


def test_corrupt_file_treated_as_empty(clock, tmp_path):
    (tmp_path / "fallback_cache.json").write_text("{not json")
    assert fc.get_working_provider() is None
    assert fc.get_dead_providers() == {}


def test_missing_file_treated_as_empty(clock, tmp_path):
    assert fc.get_working_provider() is None


def test_persisted_across_loads(clock, tmp_path):
    fc.set_working_provider("jams")
    fc.mark_dead("cerebras", "hard")
    cache_file = tmp_path / "fallback_cache.json"
    assert cache_file.exists()
    data = __import__("json").loads(cache_file.read_text())
    assert data["working"]["provider"] == "jams"
    assert "cerebras" in data["dead"]
