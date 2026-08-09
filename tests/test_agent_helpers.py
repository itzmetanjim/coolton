
import pytest
from pydantic_ai.models.openai import OpenAIChatModel

import importlib

import agent.sandbox_helpers as helpers_mod
import agent.redact as redact_mod



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
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(agent_mod, "_cached_model", None)
    return monkeypatch


# ---------------------------------------------------------------------------
# _redact
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_secret_cache(monkeypatch):
    monkeypatch.setattr(redact_mod, "_secret_values_cache", None)
    yield
    monkeypatch.setattr(redact_mod, "_secret_values_cache", None)


def test_redact_masks_known_secret(monkeypatch, fresh_secret_cache):
    monkeypatch.setenv("JAMS_API_KEY", "sk-jams-secret-123")
    redacted = agent_mod._redact("provider failed with key sk-jams-secret-123 here")
    assert "sk-jams-secret-123" not in redacted
    assert "***" in redacted


def test_redact_masks_multiple_secrets(monkeypatch, fresh_secret_cache):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa-1")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-bot-token")
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-user-token")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-1-app-token")
    msg = "key sk-oa-1 and token xoxb-bot-token plus xoxp-user-token and xapp-1-app-token"
    redacted = agent_mod._redact(msg)
    assert "sk-oa-1" not in redacted
    assert "xoxb-bot-token" not in redacted
    assert "xoxp-user-token" not in redacted
    assert "xapp-1-app-token" not in redacted


def test_redact_does_not_mask_token_shaped_strings_without_env(fresh_secret_cache):
    msg = "leaked bot=xoxb-9991336848048-11487252350550-ABCDEFGH and user=xoxp-1-abc-123"
    assert agent_mod._redact(msg) == msg


def test_redact_notifies_on_secret_hit(monkeypatch, fresh_secret_cache):
    monkeypatch.setenv("JAMS_API_KEY", "sk-jams-secret-123")
    notified = []
    redact_mod.set_notifier(lambda keys, context: notified.append((keys, context)))
    try:
        redacted = agent_mod._redact("provider key sk-jams-secret-123 leaked", context="test tool")
        assert "sk-jams-secret-123" not in redacted
        assert notified == [(["sk-jams-secret-123"], "test tool")]
    finally:
        redact_mod.set_notifier(None)


def test_redact_does_not_notify_without_secret(monkeypatch, fresh_secret_cache):
    monkeypatch.setenv("JAMS_API_KEY", "sk-jams-secret-123")
    notified = []
    redact_mod.set_notifier(lambda keys, context: notified.append((keys, context)))
    try:
        assert agent_mod._redact("nothing to hide") == "nothing to hide"
        assert notified == []
    finally:
        redact_mod.set_notifier(None)


def test_redact_leaves_other_text_alone(monkeypatch, fresh_secret_cache):
    monkeypatch.delenv("JAMS_API_KEY", raising=False)
    msg = "no secrets here, just a normal error"
    assert agent_mod._redact(msg) == msg


def test_redact_ignores_empty_values(monkeypatch, fresh_secret_cache):
    monkeypatch.setenv("JAMS_API_KEY", "")
    assert agent_mod._redact("nothing to hide") == "nothing to hide"


# ---------------------------------------------------------------------------
# enforce_rate_limit
# ---------------------------------------------------------------------------


class _FakeTime:
    def __init__(self):
        self.now = 100.0
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)


def test_rate_limit_no_sleep_on_first_call(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(agent_mod, "time", fake)
    monkeypatch.setattr(agent_mod, "_last_request_time", 0.0)
    agent_mod.enforce_rate_limit()
    assert fake.slept == []


def test_rate_limit_sleeps_when_called_again_too_soon(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(agent_mod, "time", fake)
    monkeypatch.setattr(agent_mod, "_last_request_time", 0.0)
    agent_mod.enforce_rate_limit()  # 100.0, no sleep
    fake.now = 105.0  # 5s later < 15s
    agent_mod.enforce_rate_limit()
    assert len(fake.slept) == 1
    assert fake.slept[0] == pytest.approx(10.0)


def test_rate_limit_no_sleep_after_interval_elapsed(monkeypatch):
    fake = _FakeTime()
    monkeypatch.setattr(agent_mod, "time", fake)
    monkeypatch.setattr(agent_mod, "_last_request_time", 0.0)
    agent_mod.enforce_rate_limit()  # at t=100
    fake.now = 120.0  # 20s later >= 15s
    agent_mod.enforce_rate_limit()
    assert fake.slept == []


# ---------------------------------------------------------------------------
# _apply_provider_env
# ---------------------------------------------------------------------------


def test_apply_provider_env_mapping(monkeypatch, clean_env):
    agent_mod._apply_provider_env("anthropic", "k1")
    assert __import__("os").environ["ANTHROPIC_API_KEY"] == "k1"

    agent_mod._apply_provider_env("openai", "k2")
    assert __import__("os").environ["OPENAI_API_KEY"] == "k2"

    agent_mod._apply_provider_env("jams", "k3")
    assert __import__("os").environ["OPENROUTER_API_KEY"] == "k3"

    agent_mod._apply_provider_env("gemini_gemma", "k4")
    assert __import__("os").environ["GOOGLE_API_KEY"] == "k4"

    agent_mod._apply_provider_env("mistral", "k5")
    assert __import__("os").environ["MISTRAL_API_KEY"] == "k5"

    agent_mod._apply_provider_env("groq_qwen27b", "k6")
    assert __import__("os").environ["GROQ_API_KEY"] == "k6"

    agent_mod._apply_provider_env("cerebras", "k7")
    assert __import__("os").environ["CEREBRAS_API_KEY"] == "k7"


def test_apply_provider_env_skips_byok_hcai(monkeypatch, clean_env):
    agent_mod._apply_provider_env("byok", "k")
    agent_mod._apply_provider_env("hcai", "k")
    agent_mod._apply_provider_env("hcai_luna", "k")
    os = __import__("os")
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        assert key not in os.environ


def test_apply_provider_env_empty_key_skipped(monkeypatch, clean_env):
    agent_mod._apply_provider_env("anthropic", "")
    assert "ANTHROPIC_API_KEY" not in __import__("os").environ


# ---------------------------------------------------------------------------
# _build_provider_order
# ---------------------------------------------------------------------------


def test_provider_order_anthropic_only(monkeypatch, clean_env):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    order = agent_mod._build_provider_order(None)
    assert [name for name, _ in order] == ["anthropic"]


def test_provider_order_no_config(monkeypatch, clean_env):
    assert agent_mod._build_provider_order(None) == []


def test_provider_order_groq_adds_groq_entries(monkeypatch, clean_env):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    order = agent_mod._build_provider_order(None)
    names = [name for name, _ in order]
    assert "groq_qwen27b" in names
    assert "groq_oss120b" in names
    assert "groq_qwen32b" in names
    assert "groq_oss20b" in names


def test_provider_order_jams_and_hcai_interleave(monkeypatch, clean_env):
    monkeypatch.setenv("JAMS_API_KEY", "j")
    monkeypatch.setenv("HCAI_API_KEY", "h")
    order = agent_mod._build_provider_order(None)
    names = [name for name, _ in order]
    assert names[0] == "jams_luna"
    assert "hcai_luna" in names
    assert "jams" in names
    assert "hcai" in names
    assert "jams_minimax" in names
    assert "hcai_minimax" in names
    # HCAI entries carry the explicit base_url
    hcai = dict(order)["hcai"]
    assert hcai["base_url"] == "https://ai.hackclub.com/proxy/v1"


def test_provider_order_byok_first(monkeypatch, clean_env):
    monkeypatch.setattr(agent_mod, "get_user_text_endpoint", lambda uid: {"model": "m", "base_url": "https://user", "api_key": "uk"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    order = agent_mod._build_provider_order("U1")
    assert order[0][0] == "byok"
    assert order[0][1]["base_url"] == "https://user"


# ---------------------------------------------------------------------------
# get_runtime_model
# ---------------------------------------------------------------------------


def test_runtime_model_string_provider(monkeypatch, clean_env):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    model = agent_mod.get_runtime_model()
    assert model == "anthropic:claude-sonnet-4-6"


def test_runtime_model_hcai_returns_model_object(monkeypatch, clean_env):
    monkeypatch.setenv("HCAI_API_KEY", "h")
    model = agent_mod.get_runtime_model()
    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "openai/gpt-5.6-luna"


def test_runtime_model_byok_returns_model_object(monkeypatch, clean_env):
    monkeypatch.setattr(
        agent_mod,
        "get_user_text_endpoint",
        lambda uid: {"model": "custom-model", "base_url": "https://user", "api_key": "uk"},
    )
    model = agent_mod.get_runtime_model("U1")
    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "custom-model"


def test_runtime_model_no_provider_raises(monkeypatch, clean_env):
    with pytest.raises(RuntimeError, match="No AI provider"):
        agent_mod.get_runtime_model()


# ---------------------------------------------------------------------------
# proxy cache (sandbox_helpers)
# ---------------------------------------------------------------------------


def test_proxy_cache_capped(monkeypatch):
    monkeypatch.setattr(helpers_mod, "_proxy_cache", {})
    for i in range(300):
        helpers_mod._proxy_cache_set(f"sandbox-{i}", {"token": f"t{i}"})
    assert len(helpers_mod._proxy_cache) <= helpers_mod._PROXY_CACHE_MAX
    # most recent survives
    assert helpers_mod._proxy_cache_get("sandbox-299") == {"token": "t299"}
    # the very first ones were evicted
    assert helpers_mod._proxy_cache_get("sandbox-0") is None


def test_proxy_cache_get_missing(monkeypatch):
    monkeypatch.setattr(helpers_mod, "_proxy_cache", {})
    assert helpers_mod._proxy_cache_get("sandbox-nope") is None


# ---------------------------------------------------------------------------
# skills helpers
# ---------------------------------------------------------------------------


def test_build_skill_md_escapes_apostrophes():
    md = agent_mod._build_skill_md(
        "my-skill", "handles don't and it's tricky", "body text"
    )
    assert "don''t" in md
    assert "it''s" in md


def test_build_skill_md_frontmatter():
    md = agent_mod._build_skill_md("cool-skill", "A cool skill.", "Instructions here")
    assert md.startswith("---\n")
    assert "name: cool-skill" in md
    assert "description: 'A cool skill.'" in md
    assert "# Cool Skill" in md
    assert "Instructions here" in md


def test_validate_skill_md_valid():
    ok, err = agent_mod._validate_skill_md("---\nname: x\ndescription: y\n---\n\nbody")
    assert ok is True and err == ""


def test_validate_skill_md_missing_delimiters():
    ok, err = agent_mod._validate_skill_md("name: x")
    assert ok is False and "delimiters" in err


def test_validate_skill_md_requires_name_and_description():
    ok, err = agent_mod._validate_skill_md("---\nname: x\n---\n\nbody")
    assert ok is False and "required" in err


def test_safe_name_slugifies():
    assert agent_mod._safe_name("My Cool Skill!") == "my-cool-skill-"


def test_is_within_no_traversal():
    root = "/a/b/c"
    assert agent_mod._is_within("/a/b/c", root) is True
    assert agent_mod._is_within("/a/b/c/d", root) is True
    assert agent_mod._is_within("/a/b/cd", root) is False
    assert agent_mod._is_within("/a/b", root) is False


def test_resolve_skill_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "_skill_dirs", lambda: [str(tmp_path / "skills")])
    assert agent_mod._resolve_skill("../etc") is None
    assert agent_mod._resolve_skill("a/b") is None
    assert agent_mod._resolve_skill("") is None
