import importlib
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import BinaryContent, ToolReturn

agent_mod = importlib.import_module("agent.agent")


@pytest.fixture
def clean_env(monkeypatch):
    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "JAMS_API_KEY",
        "HCAI_API_KEY",
        "GROQ_API_KEY",
        "GOOGLE_API_KEY",
        "MISTRAL_API_KEY",
        "CEREBRAS_API_KEY",
        "OPENROUTER_API_KEY_FALLBACK",
        "OPENROUTER_API_KEY",
        "E2B_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# Whether a turn is vision-capable (run_agent_turn's `is_vision`, which drives
# the turn-context text and whether attached images actually get sent to the
# model) is resolved via provider_config.is_vision_model — the same
# tags-in-providers.json source of truth the computer_use gate uses. There
# used to be a second, hand-maintained substring-marker list here
# (_VISION_MODEL_MARKERS) that answered the same question and drifted out of
# sync with providers.json — e.g. it didn't recognize "z-ai/glm-5.3-flash" or
# "qwen/qwen3.6-27b" as vision-capable even though both carry the "vision"
# tag, so a real turn on either model wrongly told itself (and the user) it
# couldn't see images. See tests/test_provider_config.py for is_vision_model's
# own coverage; this file no longer needs a parallel copy of that logic.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# see_image_from_sandbox
# ---------------------------------------------------------------------------


class _FakeFiles:
    def __init__(self, content):
        self._content = content

    def read(self, path, format="text"):
        return self._content if format == "bytes" else self._content.decode("utf-8", errors="replace")


class _FakeSandbox:
    def __init__(self, content):
        self.files = _FakeFiles(content)


def _ctx():
    return SimpleNamespace(
        deps=SimpleNamespace(channel_id="C1", thread_ts="1.1", user_id="U1")
    )


def test_see_image_returns_binary_content(monkeypatch, clean_env):
    monkeypatch.setenv("E2B_API_KEY", "e2b")
    monkeypatch.setattr(agent_mod, "get_or_create_sandbox", lambda c, t: (_FakeSandbox(b"png-bytes"), {}))
    res = agent_mod.see_image_from_sandbox(_ctx(), "~/downloads/photo.png")
    assert isinstance(res, ToolReturn)
    assert res.return_value.startswith("Here is the image")
    image_parts = [p for p in res.content if isinstance(p, BinaryContent)]
    assert len(image_parts) == 1
    img = image_parts[0]
    assert img.data == b"png-bytes"
    assert img.media_type == "image/png"
    assert img.vendor_metadata == {"detail": "high"}


def test_see_image_rejects_non_image(monkeypatch, clean_env):
    monkeypatch.setenv("E2B_API_KEY", "e2b")
    res = agent_mod.see_image_from_sandbox(_ctx(), "/home/user/notes.txt")
    assert isinstance(res, ToolReturn)
    assert "unsupported file type" in res.return_value


def test_see_image_rejects_path_traversal(monkeypatch, clean_env):
    monkeypatch.setenv("E2B_API_KEY", "e2b")
    res = agent_mod.see_image_from_sandbox(_ctx(), "/home/user/../../etc/passwd.png")
    assert isinstance(res, ToolReturn)
    assert "relative paths" in res.return_value


def test_see_image_auto_creates_sandbox(monkeypatch, clean_env):
    monkeypatch.setenv("E2B_API_KEY", "e2b")
    created = []

    def fake_get_or_create(channel, thread_ts):
        created.append((channel, thread_ts))
        return _FakeSandbox(b"png-bytes"), {}

    monkeypatch.setattr(agent_mod, "get_or_create_sandbox", fake_get_or_create)
    res = agent_mod.see_image_from_sandbox(_ctx(), "~/x.png")
    assert isinstance(res, ToolReturn)
    assert res.return_value.startswith("Here is the image")
    assert created == [("C1", "1.1")]


# ---------------------------------------------------------------------------
# download_attached_images
# ---------------------------------------------------------------------------


def _fake_client(token="xoxb-t"):
    return SimpleNamespace(token=token)


def _file(**kw):
    base = {"id": "F1", "mimetype": "image/png", "name": "a.png", "url_private_download": "https://files.slack.com/a"}
    base.update(kw)
    return base


def test_download_attached_images_filters_and_fetches(monkeypatch):
    from agent.tools import vision as vision_mod

    captured = []

    def fake_get(url, headers, timeout):
        captured.append((url, headers["Authorization"]))
        return SimpleNamespace(status_code=200, content=b"img-bytes")

    monkeypatch.setattr(vision_mod.requests, "get", fake_get)
    files = [
        _file(id="F1", mimetype="image/png", name="a.png"),
        _file(id="F2", mimetype="text/plain", name="b.txt"),
        _file(id="F3", mimetype="image/svg+xml", name="c.svg"),
        _file(id="F4", mimetype="image/jpeg", name="d.jpg"),
    ]
    images = vision_mod.download_attached_images(_fake_client(), files)
    assert [i["name"] for i in images] == ["a.png", "d.jpg"]
    assert images[0]["data"] == b"img-bytes"
    assert images[0]["media_type"] == "image/png"
    assert len(captured) == 2
    assert captured[0][1] == "Bearer xoxb-t"


def test_download_attached_images_skips_non_200(monkeypatch):
    from agent.tools import vision as vision_mod

    def fake_get(url, headers, timeout):
        return SimpleNamespace(status_code=403, content=b"nope")

    monkeypatch.setattr(vision_mod.requests, "get", fake_get)
    images = vision_mod.download_attached_images(_fake_client(), [_file()])
    assert images == []
