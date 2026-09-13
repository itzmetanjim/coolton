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


def test_without_e2b_the_raw_result_is_returned_unchanged(monkeypatch):
    """No sandbox capability at all in this deployment — the raw result (which
    may be a large base64 data: URI) is the only thing left to hand back."""
    _no_e2b(monkeypatch)
    huge_b64_result = "Generated 1 image(s):\n1. data:image/png;base64," + "A" * 5000
    monkeypatch.setattr("agent.tools.image_gen.generate_image", lambda *a, **k: huge_b64_result)
    result = agent_mod.generate_image_tool(_ctx(), prompt="a cat")
    assert result == huge_b64_result


def test_with_e2b_the_image_is_saved_to_sandbox_and_raw_bytes_never_returned(monkeypatch):
    """The whole point: a huge base64 data: URI must never reach the model's
    context once a sandbox can hold the actual file instead."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    huge_b64 = "A" * 5000
    monkeypatch.setattr(
        "agent.tools.image_gen.generate_image",
        lambda *a, **k: f"Generated 1 image(s):\n1. data:image/png;base64,{huge_b64}",
    )
    fake_sandbox = object()
    monkeypatch.setattr(agent_mod, "get_or_create_sandbox", lambda channel_id, thread_ts: (fake_sandbox, {}))
    monkeypatch.setattr(
        "agent.tools.image_gen.save_images_to_sandbox",
        lambda sandbox, urls: ["~/downloads/coolton-image-1.png"] if sandbox is fake_sandbox else [],
    )

    result = agent_mod.generate_image_tool(_ctx(), prompt="a cat")

    assert huge_b64 not in result
    assert "~/downloads/coolton-image-1.png" in result
    assert "Generated 1 image(s), saved to the following files" in result


def test_sandbox_creation_failure_falls_back_to_the_raw_result(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    raw_result = "Generated 1 image(s):\n1. https://img.example/a.png"
    monkeypatch.setattr("agent.tools.image_gen.generate_image", lambda *a, **k: raw_result)

    def _boom(channel_id, thread_ts):
        raise RuntimeError("no sandbox for you")

    monkeypatch.setattr(agent_mod, "get_or_create_sandbox", _boom)
    result = agent_mod.generate_image_tool(_ctx(), prompt="a cat")
    assert result == raw_result


def test_empty_save_result_falls_back_to_the_raw_result(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    raw_result = "Generated 1 image(s):\n1. https://img.example/a.png"
    monkeypatch.setattr("agent.tools.image_gen.generate_image", lambda *a, **k: raw_result)
    monkeypatch.setattr(agent_mod, "get_or_create_sandbox", lambda channel_id, thread_ts: (object(), {}))
    monkeypatch.setattr("agent.tools.image_gen.save_images_to_sandbox", lambda sandbox, urls: [])

    result = agent_mod.generate_image_tool(_ctx(), prompt="a cat")
    assert result == raw_result


def test_get_or_create_sandbox_is_called_even_without_a_prior_thread_sandbox(monkeypatch):
    """Regression: the old code only saved to sandbox if one already existed
    for the thread (get_thread_sandbox_id); it must now start one instead of
    silently dumping raw bytes when there isn't one yet."""
    monkeypatch.setenv("E2B_API_KEY", "test-key")
    monkeypatch.setattr(
        "agent.tools.image_gen.generate_image",
        lambda *a, **k: "Generated 1 image(s):\n1. data:image/png;base64,AAAA",
    )
    calls = []

    def fake_get_or_create(channel_id, thread_ts):
        calls.append((channel_id, thread_ts))
        return object(), {}

    monkeypatch.setattr(agent_mod, "get_or_create_sandbox", fake_get_or_create)
    monkeypatch.setattr("agent.tools.image_gen.save_images_to_sandbox", lambda sandbox, urls: ["~/downloads/x.png"])

    agent_mod.generate_image_tool(_ctx(), prompt="a cat")
    assert calls == [("C1", "1.1")]
