import json

import pytest

from agent import provider_config


def test_get_all_tags_matches_providers_json():
    # These three are the ones configured today (luna, glm5.2, glm5.3-flash) —
    # update this if providers.json's tags are intentionally changed.
    assert provider_config.get_all_tags() == ["glm5.2", "glm5.3-flash", "luna"]


def test_extract_tag_directive_no_directive_is_unchanged():
    text, tag, error = provider_config.extract_tag_directive("just a normal message")
    assert text == "just a normal message"
    assert tag is None
    assert error is None


def test_extract_tag_directive_strips_valid_directive():
    text, tag, error = provider_config.extract_tag_directive("hello [!WITH:luna] world")
    assert text == "hello  world"
    assert tag == "luna"
    assert error is None


def test_extract_tag_directive_is_case_insensitive():
    text, tag, error = provider_config.extract_tag_directive("[!WITH:LUNA] hi")
    assert tag == "luna"
    assert error is None


def test_extract_tag_directive_strips_surrounding_whitespace_in_tag():
    text, tag, error = provider_config.extract_tag_directive("[!WITH: luna ] hi")
    assert tag == "luna"
    assert error is None


def test_extract_tag_directive_escaped_strips_only_backslash():
    text, tag, error = provider_config.extract_tag_directive(r"hello \[!WITH:luna] world")
    assert text == "hello [!WITH:luna] world"
    assert tag is None
    assert error is None


def test_extract_tag_directive_unknown_tag_returns_error():
    text, tag, error = provider_config.extract_tag_directive("[!WITH:bogus] hi")
    assert tag is None
    assert error is not None
    assert "bogus" in error
    assert "luna" in error and "glm5.2" in error and "glm5.3-flash" in error
    assert r"\[!WITH:bogus]" in error


def test_extract_tag_directive_escaped_unknown_tag_is_not_an_error():
    text, tag, error = provider_config.extract_tag_directive(r"\[!WITH:bogus] hi")
    assert text == "[!WITH:bogus] hi"
    assert tag is None
    assert error is None


def test_extract_tag_directive_only_first_live_directive_wins():
    text, tag, error = provider_config.extract_tag_directive("[!WITH:luna] and also [!WITH:glm5.2]")
    assert tag == "luna"
    assert error is None
    assert "[!WITH:" not in text


def test_every_configured_model_declares_a_context_window():
    """history_compaction.py sizes its compaction budget off the smallest
    reachable model's context_window — a model added without one silently
    falls back to a generic default instead of actually protecting that
    model's real limit."""
    models = provider_config._get_models()
    missing = [m["model"] for m in models if not m.get("context_window")]
    assert missing == []


# ---------------------------------------------------------------------------
# get_min_context_window — isolated against a throwaway providers.json so
# these don't depend on (or accidentally mutate expectations about) the real
# one, or on which real provider env vars happen to be set.
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config(tmp_path):
    def _write(data: dict):
        path = tmp_path / "providers.json"
        path.write_text(json.dumps(data))
        provider_config._load_config(str(path))
        return path

    yield _write
    provider_config._reset()


def test_get_min_context_window_returns_the_smallest_reachable(isolated_config, monkeypatch):
    isolated_config({
        "providers": [
            {"id": "p1", "api_url": None, "api_key_env_var_name": "P1_KEY"},
            {"id": "p2", "api_url": None, "api_key_env_var_name": "P2_KEY"},
        ],
        "models": [
            {"provider": "p1", "model": "m1", "context_window": 200_000},
            {"provider": "p2", "model": "m2", "context_window": 100_000},
        ],
    })
    monkeypatch.setenv("P1_KEY", "k")
    monkeypatch.setenv("P2_KEY", "k")
    assert provider_config.get_min_context_window() == 100_000


def test_get_min_context_window_skips_unreachable_providers(isolated_config, monkeypatch):
    isolated_config({
        "providers": [
            {"id": "p1", "api_url": None, "api_key_env_var_name": "P1_KEY"},
            {"id": "p2", "api_url": None, "api_key_env_var_name": "P2_KEY"},
        ],
        "models": [
            {"provider": "p1", "model": "m1", "context_window": 200_000},
            {"provider": "p2", "model": "m2", "context_window": 100_000},
        ],
    })
    monkeypatch.setenv("P1_KEY", "k")
    monkeypatch.delenv("P2_KEY", raising=False)
    # p2's smaller window doesn't count — its key isn't set, so it can't
    # actually serve this turn.
    assert provider_config.get_min_context_window() == 200_000


def test_get_min_context_window_respects_tag_filter(isolated_config, monkeypatch):
    isolated_config({
        "providers": [{"id": "p1", "api_url": None, "api_key_env_var_name": "P1_KEY"}],
        "models": [
            {"provider": "p1", "model": "m1", "context_window": 200_000, "tags": ["luna"]},
            {"provider": "p1", "model": "m2", "context_window": 50_000},
        ],
    })
    monkeypatch.setenv("P1_KEY", "k")
    assert provider_config.get_min_context_window(tag="luna") == 200_000


def test_get_min_context_window_falls_back_to_default_when_nothing_reachable(isolated_config, monkeypatch):
    isolated_config({
        "providers": [{"id": "p1", "api_url": None, "api_key_env_var_name": "P1_KEY"}],
        "models": [{"provider": "p1", "model": "m1", "context_window": 200_000}],
    })
    monkeypatch.delenv("P1_KEY", raising=False)
    assert provider_config.get_min_context_window(default=99_000) == 99_000


def test_get_min_context_window_ignores_models_missing_the_field(isolated_config, monkeypatch):
    isolated_config({
        "providers": [{"id": "p1", "api_url": None, "api_key_env_var_name": "P1_KEY"}],
        "models": [
            {"provider": "p1", "model": "m1"},
            {"provider": "p1", "model": "m2", "context_window": 77_000},
        ],
    })
    monkeypatch.setenv("P1_KEY", "k")
    assert provider_config.get_min_context_window() == 77_000
