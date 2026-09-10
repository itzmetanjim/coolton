"""agent.agent.generate_image_tool — the @agent.tool wrapper around
agent.tools.image_gen.generate_image. Only concerned with the wrapper's own
logic (quality validation/normalization, argument passthrough) —
agent.tools.image_gen itself is covered by tests/test_image_gen.py.
"""

import importlib
from types import SimpleNamespace

from pydantic_ai import RunContext

agent_mod = importlib.import_module("agent.agent")


def _ctx():
    deps = SimpleNamespace(user_id="U1", channel_id="C1", thread_ts="1.1")
    return RunContext(model=None, usage=None, prompt="", deps=deps)


def _no_e2b(monkeypatch):
    # Keep the sandbox-save branch out of scope for these tests — it's
    # exercised implicitly by whatever generate_image returns.
    monkeypatch.delenv("E2B_API_KEY", raising=False)


def test_delegates_with_defaults(monkeypatch):
    _no_e2b(monkeypatch)
    called = {}
    monkeypatch.setattr(
        "agent.tools.image_gen.generate_image",
        lambda user_id, prompt, n, size, aspect_ratio, quality: called.update(
            user_id=user_id, prompt=prompt, n=n, size=size,
            aspect_ratio=aspect_ratio, quality=quality,
        ) or "Generated 1 image(s):\n1. https://img.example/a.png",
    )
    result = agent_mod.generate_image_tool(_ctx(), prompt="a cat")
    assert "image(s)" in result
    assert called == {
        "user_id": "U1", "prompt": "a cat", "n": 1, "size": "1024x1024",
        "aspect_ratio": None, "quality": "low",
    }


def test_quality_is_normalized_and_passed_through(monkeypatch):
    _no_e2b(monkeypatch)
    called = {}
    monkeypatch.setattr(
        "agent.tools.image_gen.generate_image",
        lambda user_id, prompt, n, size, aspect_ratio, quality: called.update(quality=quality) or "Generated 1 image(s):\n1. x",
    )
    agent_mod.generate_image_tool(_ctx(), prompt="a cat", quality=" HIGH ")
    assert called["quality"] == "high"


def test_invalid_quality_is_rejected_before_calling_generate_image(monkeypatch):
    _no_e2b(monkeypatch)
    called = {"hit": False}
    monkeypatch.setattr(
        "agent.tools.image_gen.generate_image",
        lambda *a, **k: called.update(hit=True) or "Generated 1 image(s):\n1. x",
    )
    result = agent_mod.generate_image_tool(_ctx(), prompt="a cat", quality="medium")
    assert "Error" in result
    assert "high" in result and "low" in result
    assert called["hit"] is False


def test_aspect_ratio_passed_as_none_when_empty(monkeypatch):
    _no_e2b(monkeypatch)
    called = {}
    monkeypatch.setattr(
        "agent.tools.image_gen.generate_image",
        lambda user_id, prompt, n, size, aspect_ratio, quality: called.update(aspect_ratio=aspect_ratio) or "Generated 1 image(s):\n1. x",
    )
    agent_mod.generate_image_tool(_ctx(), prompt="a cat", aspect_ratio="16:9")
    assert called["aspect_ratio"] == "16:9"
