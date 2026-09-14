"""agent.tools.image_gen.generate_image — BYOK -> HCAI (quality-ordered,
falls back to the other quality) -> global OPENAI_API_KEY, in that order.
"""

from unittest.mock import patch

import pytest

from agent.tools import image_gen


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_byok(monkeypatch):
    monkeypatch.setattr(image_gen, "get_image_endpoint_id", lambda user_id: None)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _isolated_fallback_cache(monkeypatch, tmp_path):
    """generate_image now reads/writes agent.fallback_cache's family-dead
    state (see the daily-spending-limit tests below) — isolate it from the
    real gitignored fallback_cache.json so tests never touch production
    state or leak marks between tests."""
    from agent import fallback_cache
    monkeypatch.setattr(fallback_cache, "FALLBACK_CACHE_FILE", str(tmp_path / "fallback_cache.json"))


def test_byok_endpoint_used_when_set_and_quality_ignored(monkeypatch):
    monkeypatch.setattr(image_gen, "get_image_endpoint_id", lambda user_id: "ep1")
    monkeypatch.setattr(
        image_gen, "get_endpoint_decrypted",
        lambda user_id, ep_id: {"api_key": "byok-key", "base_url": "https://byok.example/v1", "model": "byok-model"},
    )
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["url"] = url
        captured["model"] = json["model"]
        return _FakeResp({"data": [{"url": "https://img.example/byok.png"}]})

    with patch.object(image_gen.requests, "post", side_effect=fake_post):
        result = image_gen.generate_image("U1", "a cat", quality="high")

    assert "image(s)" in result
    assert captured["url"] == "https://byok.example/v1/images/generations"
    assert captured["model"] == "byok-model"


def test_no_byok_falls_through_to_hcai_high_quality(monkeypatch):
    from agent import provider_config
    monkeypatch.setattr(
        provider_config, "build_image_provider_order",
        lambda quality: [
            {"model": "pro-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "high", "provider": "hcai"},
            {"model": "flash-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "low", "provider": "hcai"},
        ] if quality == "high" else [],
    )
    captured = {}

    def fake_post(url, json, headers, timeout):
        captured["model"] = json["model"]
        return _FakeResp({"data": [{"url": "https://img.example/pro.png"}]})

    with patch.object(image_gen.requests, "post", side_effect=fake_post):
        result = image_gen.generate_image("U1", "a cat", quality="high")

    assert "image(s)" in result
    assert captured["model"] == "pro-model"


def test_falls_back_to_the_other_quality_when_primary_fails(monkeypatch):
    from agent import provider_config
    monkeypatch.setattr(
        provider_config, "build_image_provider_order",
        lambda quality: [
            {"model": "pro-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "high", "provider": "hcai"},
            {"model": "flash-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "low", "provider": "hcai"},
        ],
    )
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(json["model"])
        if json["model"] == "pro-model":
            return _FakeResp({"error": {"message": "HCAI is down"}})
        return _FakeResp({"data": [{"url": "https://img.example/flash.png"}]})

    with patch.object(image_gen.requests, "post", side_effect=fake_post):
        result = image_gen.generate_image("U1", "a cat", quality="high")

    assert "image(s)" in result
    assert calls == ["pro-model", "flash-model"]


def test_falls_back_to_global_openai_key_when_both_hcai_models_fail(monkeypatch):
    from agent import provider_config
    monkeypatch.setattr(
        provider_config, "build_image_provider_order",
        lambda quality: [
            {"model": "pro-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "high", "provider": "hcai"},
        ],
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-global")
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append((url, json["model"]))
        if json["model"] == "pro-model":
            return _FakeResp({"error": {"message": "down"}})
        return _FakeResp({"data": [{"url": "https://img.example/dalle.png"}]})

    with patch.object(image_gen.requests, "post", side_effect=fake_post):
        result = image_gen.generate_image("U1", "a cat", quality="high")

    assert "image(s)" in result
    assert calls[-1] == ("https://api.openai.com/v1/images/generations", "dall-e-3")


def test_returns_the_last_error_when_nothing_succeeds(monkeypatch):
    from agent import provider_config
    monkeypatch.setattr(
        provider_config, "build_image_provider_order",
        lambda quality: [
            {"model": "pro-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "high", "provider": "hcai"},
        ],
    )

    def fake_post(url, json, headers, timeout):
        return _FakeResp({"error": {"message": "still down"}})

    with patch.object(image_gen.requests, "post", side_effect=fake_post):
        result = image_gen.generate_image("U1", "a cat", quality="high")

    assert "still down" in result
    assert "image(s)" not in result


def test_no_byok_no_hcai_no_global_key_reports_a_clear_error(monkeypatch):
    from agent import provider_config
    monkeypatch.setattr(provider_config, "build_image_provider_order", lambda quality: [])
    result = image_gen.generate_image("U1", "a cat")
    assert "Error" in result
    assert "BYOK" in result


# ---------------------------------------------------------------------------
# HCAI's daily spending cap (a family-wide outage, not a per-model one — see
# agent.fallback_cache.mark_family_dead and the matching chat-side check in
# agent.agent._run_with_provider_chain) — skip the REST of HCAI immediately
# rather than trying every remaining HCAI model one by one.
# ---------------------------------------------------------------------------


def test_daily_spending_limit_marks_hcai_dead_and_falls_through_to_openai(monkeypatch):
    from agent import fallback_cache, provider_config
    monkeypatch.setattr(
        provider_config, "build_image_provider_order",
        lambda quality: [
            {"model": "pro-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "high", "provider": "hcai"},
        ],
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-global")
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(json["model"])
        if json["model"] == "pro-model":
            return _FakeResp({"error": {"message": "Daily spending limit of $3 reached. Need a higher limit? hey@mahadk.com"}})
        return _FakeResp({"data": [{"url": "https://img.example/dalle.png"}]})

    with patch.object(image_gen.requests, "post", side_effect=fake_post):
        result = image_gen.generate_image("U1", "a cat", quality="high")

    assert "image(s)" in result
    assert calls == ["pro-model", "dall-e-3"]
    assert "hcai" in fallback_cache.get_dead_families()


def test_daily_spending_limit_on_first_hcai_model_skips_the_second_hcai_model_too(monkeypatch):
    """Both quality tiers route through the same HCAI account — once the cap
    is hit on the first, the second must be skipped outright, not attempted
    and independently hit the same cap."""
    from agent import provider_config
    monkeypatch.setattr(
        provider_config, "build_image_provider_order",
        lambda quality: [
            {"model": "pro-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "high", "provider": "hcai"},
            {"model": "flash-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "low", "provider": "hcai"},
        ],
    )
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(json["model"])
        return _FakeResp({"error": {"message": "Daily spending limit of $3 reached. Need a higher limit? hey@mahadk.com"}})

    with patch.object(image_gen.requests, "post", side_effect=fake_post):
        result = image_gen.generate_image("U1", "a cat", quality="high")

    assert calls == ["pro-model"]  # flash-model never even attempted
    assert "image(s)" not in result


def test_hcai_already_marked_dead_is_skipped_without_any_request(monkeypatch):
    from agent import fallback_cache, provider_config
    fallback_cache.mark_family_dead("hcai", "daily spending limit of $3 reached")
    monkeypatch.setattr(
        provider_config, "build_image_provider_order",
        lambda quality: [
            {"model": "pro-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "high", "provider": "hcai"},
        ],
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-global")
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(json["model"])
        return _FakeResp({"data": [{"url": "https://img.example/dalle.png"}]})

    with patch.object(image_gen.requests, "post", side_effect=fake_post):
        result = image_gen.generate_image("U1", "a cat", quality="high")

    assert "image(s)" in result
    assert calls == ["dall-e-3"]  # pro-model (hcai) never attempted


def test_an_unrelated_error_does_not_mark_the_family_dead(monkeypatch):
    from agent import fallback_cache, provider_config
    monkeypatch.setattr(
        provider_config, "build_image_provider_order",
        lambda quality: [
            {"model": "pro-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "high", "provider": "hcai"},
        ],
    )

    def fake_post(url, json, headers, timeout):
        return _FakeResp({"error": {"message": "some other transient error"}})

    with patch.object(image_gen.requests, "post", side_effect=fake_post):
        image_gen.generate_image("U1", "a cat", quality="high")

    assert fallback_cache.get_dead_families() == {}
