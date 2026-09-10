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
            {"model": "pro-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "high"},
            {"model": "flash-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "low"},
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
            {"model": "pro-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "high"},
            {"model": "flash-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "low"},
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
            {"model": "pro-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "high"},
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
            {"model": "pro-model", "base_url": "https://hcai.example/v1", "api_key": "hk", "display": "high"},
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
