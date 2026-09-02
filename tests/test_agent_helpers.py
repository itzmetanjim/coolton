
import time

import pytest
from pydantic_ai.models.openai import OpenAIChatModel
from unittest.mock import Mock, patch

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
        assert notified == [(["JAMS_API_KEY"], "test tool")]
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


def test_redact_masks_hardcoded_canary(fresh_secret_cache):
    msg = "endpoint returned COOLTON-CANARY-c7e6f357-5197-4d5e-8682-9e0758561d8f here"
    redacted = agent_mod._redact(msg)
    assert "COOLTON-CANARY-c7e6f357-5197-4d5e-8682-9e0758561d8f" not in redacted
    assert "***" in redacted


def test_strip_secret_keys_removes_token_fields():
    from agent.redact import strip_secret_keys

    obj = {
        "ok": True,
        "args": {"token": "xoxp-1", "nested": {"user_token": "xoxp-2", "keep": "yes"}},
        "secret": "s",
        "password": "p",
        "fine": [{"token": "xoxp-3"}, "text"],
    }
    result = strip_secret_keys(obj)
    assert "token" not in str(result)
    assert "xoxp" not in str(result)
    assert result == {"ok": True, "args": {"nested": {"keep": "yes"}}, "fine": [{}, "text"]}


def test_agent_hooks_redact_tool_result_and_output(monkeypatch, fresh_secret_cache):
    monkeypatch.setenv("JAMS_API_KEY", "sk-jams-secret-123")
    from pydantic_ai import RunContext
    from pydantic_ai.messages import ToolCallPart
    from types import SimpleNamespace

    ctx = RunContext(model=None, usage=None, prompt="", deps=None)
    tool_def = SimpleNamespace(name="run_linux_command")
    call = ToolCallPart(tool_name="run_linux_command", args={})

    tool_result = agent_mod._redact_tool_result(
        ctx, call=call, tool_def=tool_def, args={}, result="STDOUT sk-jams-secret-123 here"
    )
    assert "sk-jams-secret-123" not in tool_result
    assert "***" in tool_result

    output = agent_mod._redact_output(ctx, output_context=None, output="final sk-jams-secret-123")
    assert "sk-jams-secret-123" not in output
    assert "***" in output

    non_str = agent_mod._redact_tool_result(
        ctx, call=call, tool_def=tool_def, args={}, result={"key": "sk-jams-secret-123"}
    )
    assert non_str == {"key": "sk-jams-secret-123"}


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

    agent_mod._apply_provider_env("gemini_gemma", "k4")
    assert __import__("os").environ["GOOGLE_API_KEY"] == "k4"

    agent_mod._apply_provider_env("mistral", "k5")
    assert __import__("os").environ["MISTRAL_API_KEY"] == "k5"

    # "groq_1" is the shape providers.json actually generates for the 2nd of
    # several groq models (see _make_provider_name) — exact-match on the base
    # id after stripping the trailing "_<index>".
    agent_mod._apply_provider_env("groq_1", "k6")
    assert __import__("os").environ["GROQ_API_KEY"] == "k6"


def test_apply_provider_env_skips_byok_hcai(monkeypatch, clean_env):
    agent_mod._apply_provider_env("byok", "k")
    agent_mod._apply_provider_env("hcai", "k")
    agent_mod._apply_provider_env("hcai_luna", "k")
    os = __import__("os")
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
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


def test_provider_order_hcai_models(monkeypatch, clean_env):
    monkeypatch.setenv("HCAI_API_KEY", "h")
    order = agent_mod._build_provider_order(None)
    names = [name for name, _ in order]
    assert names[0] == "hcai_0"
    assert "hcai_1" in names
    assert "hcai_2" in names
    assert "hcai_3" in names
    # HCAI entries carry the explicit base_url
    hcai = dict(order)["hcai_2"]
    assert hcai["base_url"] == "https://ai.hackclub.com/proxy/v1"


def test_provider_order_byok_first(monkeypatch, clean_env):
    monkeypatch.setattr(agent_mod, "get_user_text_endpoint", lambda uid: {"model": "m", "base_url": "https://user", "api_key": "uk"})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    order = agent_mod._build_provider_order("U1")
    assert order[0][0] == "byok"
    assert order[0][1]["base_url"] == "https://user"


def test_provider_order_tag_filter_restricts_to_tagged_models(monkeypatch, clean_env):
    monkeypatch.setenv("HCAI_API_KEY", "h")
    monkeypatch.setenv("OPENROUTER_API_KEY_FALLBACK", "or")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    order = agent_mod._build_provider_order(None, tag="luna")
    from agent.provider_config import _get_models
    assert all("luna" in (m.get("tags") or []) for name, cfg in order for m in _get_models() if m["model"] == cfg["model"])
    # anthropic (untagged) must not appear when a tag filter is active
    assert "anthropic" not in [name for name, _ in order]


def test_provider_order_tag_filter_matches_multiple_providers(monkeypatch, clean_env):
    monkeypatch.setenv("HCAI_API_KEY", "h")
    monkeypatch.setenv("OPENROUTER_API_KEY_FALLBACK", "or")
    order = agent_mod._build_provider_order(None, tag="glm5.2")
    models = [cfg["model"] for _, cfg in order]
    assert "z-ai/glm-5.2:free" in models
    assert "openrouter:z-ai/glm-5.2:free" in models


def test_provider_order_tag_filter_vision_matches_configured_vision_models(monkeypatch, clean_env):
    # computer_use's vision gate (provider_config.is_vision_model) depends on this
    # filter reaching exactly the models tagged "vision" in providers.json — every
    # provider that has a vision-tagged entry needs its key set for a complete check.
    monkeypatch.setenv("HCAI_API_KEY", "h")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("GOOGLE_API_KEY", "g")
    monkeypatch.setenv("KILOCODE_API_KEY", "kc")
    monkeypatch.setenv("OPENROUTER_API_KEY_FALLBACK", "or")
    monkeypatch.setenv("GROQ_API_KEY", "gr")
    monkeypatch.setenv("MISTRAL_API_KEY", "m")
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "oz")
    order = agent_mod._build_provider_order(None, tag="vision")
    from agent.provider_config import _get_models
    vision_models = {m["model"] for m in _get_models() if "vision" in (m.get("tags") or [])}
    assert {cfg["model"] for _, cfg in order} == vision_models
    assert vision_models  # sanity: the tag actually exists in current config


def test_provider_order_tag_filter_excludes_byok(monkeypatch, clean_env):
    monkeypatch.setattr(agent_mod, "get_user_text_endpoint", lambda uid: {"model": "m", "base_url": "https://user", "api_key": "uk"})
    monkeypatch.setenv("HCAI_API_KEY", "h")
    order = agent_mod._build_provider_order("U1", tag="luna")
    assert "byok" not in [name for name, _ in order]


def test_provider_order_unknown_tag_yields_empty_order(monkeypatch, clean_env):
    monkeypatch.setenv("HCAI_API_KEY", "h")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert agent_mod._build_provider_order(None, tag="nonexistent-tag") == []


def test_resolve_provider_order_tag_skips_fallback_cache_reordering(monkeypatch, clean_env):
    """A forced tag is deterministic — the fallback cache's "prefer last known
    working provider" reordering must not silently reintroduce a provider
    the tag already excluded, or reorder within it unexpectedly."""
    monkeypatch.setenv("HCAI_API_KEY", "h")
    monkeypatch.setenv("OPENROUTER_API_KEY_FALLBACK", "or")
    monkeypatch.setattr("agent.fallback_cache.get_dead_providers", lambda: {"hcai_0"})
    monkeypatch.setattr("agent.fallback_cache.get_working_provider", lambda: "openrouter_fb_1")

    untagged_order = agent_mod._resolve_provider_order(None)
    tagged_order = agent_mod._resolve_provider_order(None, tag="luna")

    # Without a tag, the cache logic actively filters/reorders.
    assert "hcai_0" not in [n for n, _ in untagged_order]
    # With a tag forced, the raw tag-filtered order is used untouched by the cache.
    from agent.provider_config import build_provider_order
    assert tagged_order == build_provider_order(None, tag="luna")


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
    # Asserts against the first hcai entry in providers.json's fallback order,
    # not a specific model — update this if that ordering is intentionally
    # changed again.
    from agent.provider_config import _get_models
    first_hcai_model = next(m["model"] for m in _get_models() if m["provider"] == "hcai")
    assert model.model_name == first_hcai_model


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

@pytest.mark.parametrize(
    ("env_key", "expected"),
    [
        ("ANTHROPIC_API_KEY", "anthropic:claude-sonnet-4-6"),
        ("OPENAI_API_KEY", "openai:gpt-4.1-mini"),
        ("OPENROUTER_API_KEY_FALLBACK", "openrouter:z-ai/glm-5.3-flash"),
        ("GOOGLE_API_KEY", "google:gemma-4-31b-it"),
        ("GROQ_API_KEY", "groq:qwen/qwen3.6-27b"),
        ("MISTRAL_API_KEY", "mistral:mistral-large-2512"),
    ],
)
def test_get_model_accepts_documented_provider_keys(monkeypatch, clean_env, env_key, expected):
    monkeypatch.setenv(env_key, "test-key")
    result = agent_mod.get_model()
    if isinstance(result, OpenAIChatModel):
        assert result.model_name == expected
    else:
        assert result == expected


# ---------------------------------------------------------------------------
# chat_postMessage / slack_api_call empty-params guard
# ---------------------------------------------------------------------------


def _run_ctx(client):
    from pydantic_ai import RunContext
    from types import SimpleNamespace

    deps = SimpleNamespace(
        client=client, channel_id="C1", thread_ts="1.2", user_id="U_TEST",
        last_screenshot_post_ts=0.0, sandbox_keepalive_seconds=0.0, keep_sandbox_warm=False,
        slack_api_call_failures={},
    )
    return RunContext(model=None, usage=None, prompt="", deps=deps)


# ---------------------------------------------------------------------------
# _inject_poster — must fail CLOSED: a failed/absent identity lookup must
# strip any pre-existing username/icon_url rather than pass a spoofed value
# through untouched.
# ---------------------------------------------------------------------------


def test_inject_poster_strips_spoofed_identity_on_lookup_failure(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(agent_mod, "_user_info_cache", {})
    params = {"channel": "C1", "text": "hi", "username": "Evil Spoof", "icon_url": "http://evil/x.png"}
    result = agent_mod._inject_poster(params, "U_TEST")
    assert "username" not in result
    assert "icon_url" not in result


def test_inject_poster_strips_spoofed_identity_when_no_user_id(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(agent_mod, "_user_info_cache", {})
    params = {"channel": "C1", "text": "hi", "username": "Evil Spoof"}
    result = agent_mod._inject_poster(params, "")
    assert "username" not in result


def test_inject_poster_sets_real_identity_on_success(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(agent_mod, "_user_info_cache", {})

    def fake_get(url, **kwargs):
        return Mock(json=lambda: {
            "ok": True,
            "user": {"profile": {"display_name": "Real Name", "image_72": "http://real/pfp.png"}},
        })

    monkeypatch.setattr(agent_mod.requests, "get", fake_get)
    params = {"channel": "C1", "text": "hi", "username": "Evil Spoof", "icon_url": "http://evil/x.png"}
    result = agent_mod._inject_poster(params, "U_TEST")
    assert result["username"] == "Real Name"
    assert result["icon_url"] == "http://real/pfp.png"


def test_inject_poster_strips_spoofed_identity_on_api_exception(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(agent_mod, "_user_info_cache", {})

    def raising_get(url, **kwargs):
        raise RuntimeError("network error")

    monkeypatch.setattr(agent_mod.requests, "get", raising_get)
    params = {"channel": "C1", "text": "hi", "username": "Evil Spoof"}
    result = agent_mod._inject_poster(params, "U_TEST")
    assert "username" not in result


def test_chat_post_message_sends_as_bot():
    client = Mock()
    client.chat_postMessage.return_value = {"ok": True}
    result = agent_mod.chat_postMessage(_run_ctx(client), channel="U0B2VTYER33", text="hello")
    assert result == "Message sent."
    client.chat_postMessage.assert_called_once_with(
        channel="U0B2VTYER33", markdown_text="hello"
    )


def test_chat_post_message_includes_thread_ts_when_passed():
    client = Mock()
    client.chat_postMessage.return_value = {"ok": True}
    result = agent_mod.chat_postMessage(
        _run_ctx(client), channel="C1", text="hi", thread_ts="1.2"
    )
    assert result == "Message sent."
    client.chat_postMessage.assert_called_once_with(channel="C1", markdown_text="hi", thread_ts="1.2")


def test_chat_post_message_requires_channel_and_text():
    client = Mock()
    assert "channel is required" in agent_mod.chat_postMessage(_run_ctx(client), channel="", text="hi")
    assert "text is required" in agent_mod.chat_postMessage(_run_ctx(client), channel="C1", text="")
    client.chat_postMessage.assert_not_called()


def test_chat_post_message_reports_slack_error():
    client = Mock()
    client.chat_postMessage.return_value = {"ok": False, "error": "missing_argument"}
    result = agent_mod.chat_postMessage(_run_ctx(client), channel="C1", text="hi")
    assert "missing_argument" in result


def test_slack_api_call_rejects_chat_post_message_without_channel(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    with patch("agent.agent.requests.post") as post:
        result = agent_mod.slack_api_call(_run_ctx(Mock()), method="chat.postMessage", api_parameters="{}")
    assert "requires a 'channel'" in result
    post.assert_not_called()


def test_slack_api_call_rejects_chat_post_message_without_text(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    with patch("agent.agent.requests.post") as post:
        result = agent_mod.slack_api_call(
            _run_ctx(Mock()), method="chat.postMessage", api_parameters='{"channel": "C1"}'
        )
    assert "requires a 'text'" in result
    post.assert_not_called()


def test_slack_api_call_parses_json_string_parameters(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        result = agent_mod.slack_api_call(
            _run_ctx(Mock()), method="conversations.join", api_parameters='{"channel": "C0123456"}'
        )
    assert "Success" in result
    post.assert_called_once()
    assert post.call_args.kwargs["data"]["channel"] == "C0123456"


def test_slack_api_call_rejects_malformed_json_string(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    with patch("agent.agent.requests.post") as post:
        result = agent_mod.slack_api_call(
            _run_ctx(Mock()), method="conversations.join", api_parameters="{not valid json"
        )
    assert "must be valid JSON" in result
    post.assert_not_called()


def test_slack_api_call_rejects_a_json_array_as_parameters(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    with patch("agent.agent.requests.post") as post:
        result = agent_mod.slack_api_call(
            _run_ctx(Mock()), method="conversations.join", api_parameters='["C0123456"]'
        )
    assert "must be a JSON object" in result
    post.assert_not_called()


def test_slack_api_call_treats_blank_string_as_empty_parameters(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        result = agent_mod.slack_api_call(_run_ctx(Mock()), method="auth.test", api_parameters="")
    assert "Success" in result
    post.assert_called_once()


def test_slack_api_call_allows_empty_params_for_other_methods(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        result = agent_mod.slack_api_call(_run_ctx(Mock()), method="auth.test", api_parameters="{}")
    assert "Success" in result
    post.assert_called_once()


def test_slack_api_call_returns_full_error_json(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {
            "ok": False,
            "error": "missing_argument",
            "required": "channel",
            "provided": ["token"],
        }
        result = agent_mod.slack_api_call(
            _run_ctx(Mock()), method="conversations.info", api_parameters="{}"
        )
    assert "missing_argument" in result
    assert "required" in result
    assert "channel" in result
    assert "provided" in result


def test_slack_api_call_blocks_after_one_failure(monkeypatch):
    """The exact loop a user reported: the model kept calling conversations.join with
    an empty params dict, never correcting itself, and kept going even after an
    explicit "stop retrying this" error. The budget is tight (1) precisely because a
    softer limit didn't stop it — the 2nd identical attempt must never reach the
    network at all."""
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    ctx = _run_ctx(Mock())
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": False, "error": "missing_argument"}
        first = agent_mod.slack_api_call(ctx, method="conversations.join", api_parameters="{}")
        second = agent_mod.slack_api_call(ctx, method="conversations.join", api_parameters="{}")
    assert post.call_count == 1  # only the first attempt hits the network
    assert "missing_argument" in first
    assert "already failed" in second
    assert "api_parameters was empty" in second


def test_slack_api_call_block_is_shared_across_both_slack_api_tools(monkeypatch):
    """Observed live: a model blocked on slack_api_call_as_bot_tool immediately just
    switched to slack_api_call with the exact same broken call and got a fresh budget.
    Both tools must share one failure count per (method, params), not one each.

    agent.agent and agent.tools.slack_bot_api both `import requests` — the same module
    object (verified directly) — so a single patch of requests.post covers whichever
    of the two tools actually reaches the network below."""
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    ctx = _run_ctx(Mock())
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": False, "error": "missing_argument"}
        first = agent_mod.slack_api_call(ctx, method="conversations.join", api_parameters="{}")
        second = agent_mod.slack_api_call_as_bot_tool(ctx, method="conversations.join", api_parameters="{}")
    assert post.call_count == 1  # only the first attempt, on either tool, hits the network
    assert "missing_argument" in first
    assert "already failed" in second
    assert "share this limit" in second


def test_slack_api_call_block_is_specific_to_the_exact_method_and_params(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    ctx = _run_ctx(Mock())
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": False, "error": "missing_argument"}
        agent_mod.slack_api_call(ctx, method="conversations.join", api_parameters="{}")
        agent_mod.slack_api_call(ctx, method="conversations.join", api_parameters="{}")  # blocked
        # A different channel is a different failure signature — not blocked by the above.
        result = agent_mod.slack_api_call(ctx, method="conversations.join", api_parameters='{"channel": "C2"}')
    assert post.call_count == 2  # the genuinely different call still reaches the network
    assert "already failed" not in result


def test_slack_api_call_a_success_does_not_get_blocked_later(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    ctx = _run_ctx(Mock())
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        for _ in range(5):
            result = agent_mod.slack_api_call(ctx, method="auth.test", api_parameters="{}")
    assert post.call_count == 5
    assert "Success" in result


def test_slack_api_call_as_bot_tool_blocks_after_one_failure(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    ctx = _run_ctx(Mock())
    with patch("agent.tools.slack_bot_api.requests.post") as post:
        post.return_value.json.return_value = {"ok": False, "error": "missing_argument"}
        agent_mod.slack_api_call_as_bot_tool(ctx, method="conversations.join", api_parameters="{}")
        result = agent_mod.slack_api_call_as_bot_tool(ctx, method="conversations.join", api_parameters="{}")
    assert post.call_count == 1
    assert "already failed" in result


def test_slack_api_call_as_bot_rejects_chat_post_message_without_channel(monkeypatch):
    from agent.tools import slack_bot_api

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    with patch("agent.tools.slack_bot_api.requests.post") as post:
        result = slack_bot_api.slack_api_call_as_bot("chat.postMessage", {})
    assert "requires a 'channel'" in result
    post.assert_not_called()


# ---------------------------------------------------------------------------
# _run_with_provider_chain — live model display in the plan block
# ---------------------------------------------------------------------------


def test_run_with_provider_chain_shows_model_before_run_sync_returns(monkeypatch, clean_env):
    """The plan block used to only learn which model answered after the whole
    turn (including every tool call) finished. It must now show the model as
    soon as an attempt starts, i.e. before run_sync is even called."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        agent_mod, "_resolve_provider_order",
        lambda user_id, tag=None: [("anthropic", {"model": "anthropic:claude-sonnet-4-6", "api_key": "k"})],
    )
    monkeypatch.setattr("agent.fallback_cache.set_working_provider", lambda name: None)

    call_order = []
    monkeypatch.setattr(
        "agent.plan_block.set_model_task",
        lambda deps, model_used, status="in_progress": call_order.append(("set_model_task", model_used)),
    )

    def fake_run_sync(**kwargs):
        call_order.append(("run_sync", None))
        return SimpleNamespace(output="ok")

    fake_agent = SimpleNamespace(run_sync=fake_run_sync)
    deps = SimpleNamespace(user_id=None, provider_tag_filter=None, plan_ts="1.1", last_attempt_messages=None)

    result, provider = agent_mod._run_with_provider_chain(fake_agent, {}, deps)

    assert provider == "anthropic"
    assert call_order == [
        ("set_model_task", "anthropic / anthropic:claude-sonnet-4-6"),
        ("run_sync", None),
    ]
    assert deps.model_used == "anthropic / anthropic:claude-sonnet-4-6"


def test_run_with_provider_chain_updates_model_task_again_on_fallback(monkeypatch, clean_env):
    from types import SimpleNamespace

    monkeypatch.setattr(
        agent_mod, "_resolve_provider_order",
        lambda user_id, tag=None: [
            ("hcai_0", {"model": "openai/gpt-5.6-luna", "api_key": "k"}),
            ("anthropic", {"model": "anthropic:claude-sonnet-4-6", "api_key": "k"}),
        ],
    )
    monkeypatch.setattr("agent.fallback_cache.set_working_provider", lambda name: None)
    monkeypatch.setattr("agent.fallback_cache.mark_dead", lambda name, err: None)

    shown_models = []
    monkeypatch.setattr(
        "agent.plan_block.set_model_task",
        lambda deps, model_used, status="in_progress": shown_models.append(model_used),
    )

    calls = {"n": 0}

    def fake_run_sync(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("401 unauthorized")  # hard error, no retry, falls to next provider
        return SimpleNamespace(output="ok")

    fake_agent = SimpleNamespace(run_sync=fake_run_sync)
    deps = SimpleNamespace(user_id=None, provider_tag_filter=None, plan_ts="1.1", last_attempt_messages=None)

    result, provider = agent_mod._run_with_provider_chain(fake_agent, {}, deps)

    assert provider == "anthropic"
    assert shown_models == ["hcai_0 / openai/gpt-5.6-luna", "anthropic / anthropic:claude-sonnet-4-6"]


def test_run_with_provider_chain_resumes_from_checkpoint_after_mid_turn_fallback(monkeypatch, clean_env):
    """If a provider fails AFTER real tool calls already ran this turn (agent.plan_block's
    before_tool_execute hook checkpoints progress into deps.last_attempt_messages on every
    call), the next fallback attempt must resume from that checkpoint instead of silently
    restarting the turn from its original pre-tool-call history — otherwise the fallback
    model has no memory of what already happened and the turn looks like it "reset"."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        agent_mod, "_resolve_provider_order",
        lambda user_id, tag=None: [
            ("hcai_0", {"model": "openai/gpt-5.6-luna", "api_key": "k"}),
            ("anthropic", {"model": "anthropic:claude-sonnet-4-6", "api_key": "k"}),
        ],
    )
    monkeypatch.setattr("agent.fallback_cache.set_working_provider", lambda name: None)
    monkeypatch.setattr("agent.fallback_cache.mark_dead", lambda name, err: None)
    monkeypatch.setattr("agent.plan_block.set_model_task", lambda *a, **k: None)

    checkpoint = ["fake-partial-history-after-a-tool-call"]
    deps = SimpleNamespace(user_id=None, provider_tag_filter=None, plan_ts="1.1", last_attempt_messages=None)
    seen_kwargs = []

    def fake_run_sync(**kwargs):
        seen_kwargs.append(dict(kwargs))
        if len(seen_kwargs) == 1:
            # Simulate the hook having fired for a tool call this attempt already made
            # before the model then failed on its next completion.
            deps.last_attempt_messages = checkpoint
            raise RuntimeError("401 unauthorized")  # hard error, falls to next provider
        return SimpleNamespace(output="ok")

    fake_agent = SimpleNamespace(run_sync=fake_run_sync)
    run_kwargs = {"user_prompt": "original prompt", "message_history": ["original history"]}

    result, provider = agent_mod._run_with_provider_chain(fake_agent, run_kwargs, deps)

    assert provider == "anthropic"
    assert len(seen_kwargs) == 2
    assert seen_kwargs[0]["message_history"] == ["original history"]
    assert seen_kwargs[0]["user_prompt"] == "original prompt"
    # The fallback attempt resumes from the checkpoint, not the turn's original history.
    assert seen_kwargs[1]["message_history"] is checkpoint
    assert seen_kwargs[1]["user_prompt"] is None


def test_run_with_provider_chain_does_not_touch_history_without_progress(monkeypatch, clean_env):
    """No tool call ran before the failure (deps.last_attempt_messages never got
    reassigned, e.g. an immediate auth error, or a subagent/kevinton caller that never
    wires up the hook at all) — the next attempt must use the original run_kwargs
    unchanged, not silently swap in an unrelated/stale checkpoint."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        agent_mod, "_resolve_provider_order",
        lambda user_id, tag=None: [
            ("hcai_0", {"model": "openai/gpt-5.6-luna", "api_key": "k"}),
            ("anthropic", {"model": "anthropic:claude-sonnet-4-6", "api_key": "k"}),
        ],
    )
    monkeypatch.setattr("agent.fallback_cache.set_working_provider", lambda name: None)
    monkeypatch.setattr("agent.fallback_cache.mark_dead", lambda name, err: None)
    monkeypatch.setattr("agent.plan_block.set_model_task", lambda *a, **k: None)

    # A stale checkpoint left over from something unrelated (e.g. a prior subagent call
    # sharing this same deps) must never leak into this attempt's fallback.
    stale_checkpoint = ["stale, unrelated history"]
    deps = SimpleNamespace(user_id=None, provider_tag_filter=None, plan_ts="1.1", last_attempt_messages=stale_checkpoint)
    seen_kwargs = []

    def fake_run_sync(**kwargs):
        seen_kwargs.append(dict(kwargs))
        if len(seen_kwargs) == 1:
            raise RuntimeError("401 unauthorized")
        return SimpleNamespace(output="ok")

    fake_agent = SimpleNamespace(run_sync=fake_run_sync)
    run_kwargs = {"user_prompt": "original prompt", "message_history": ["original history"]}

    result, provider = agent_mod._run_with_provider_chain(fake_agent, run_kwargs, deps)

    assert provider == "anthropic"
    assert seen_kwargs[1]["message_history"] == ["original history"]
    assert seen_kwargs[1]["user_prompt"] == "original prompt"


def test_run_with_provider_chain_includes_raw_http_body_in_all_errors(monkeypatch, clean_env):
    """A pydantic ValidationError on the response (e.g. "3 validation errors for
    ChatCompletion ... input_value=None") only says the SDK couldn't parse a
    ChatCompletion out of it — not what the base_url provider actually sent back.
    Same raw-body capture as provider_probe.test_provider, now wired into the real
    turn path too (observed live: HCAI/OpenRouter disguising a 429 as HTTP 200 with
    an error payload instead of a real ChatCompletion)."""
    import httpx
    from pydantic_ai import Agent

    monkeypatch.setenv("HCAI_API_KEY", "k")
    monkeypatch.setattr(
        agent_mod, "_resolve_provider_order",
        lambda user_id, tag=None: [
            ("hcai_0", {"model": "openai/gpt-5.6-luna", "api_key": "k", "base_url": "https://fake.example/v1"}),
        ],
    )
    monkeypatch.setattr("agent.fallback_cache.set_working_provider", lambda name: None)
    monkeypatch.setattr("agent.fallback_cache.mark_dead", lambda name, err: None)
    monkeypatch.setattr("agent.plan_block.set_model_task", lambda *a, **k: None)

    def handler(request):
        return httpx.Response(
            200,
            json={"id": "gen-fake", "error": {"message": "temporarily rate-limited upstream", "code": 429}},
        )

    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)

    from types import SimpleNamespace
    deps = SimpleNamespace(user_id=None, provider_tag_filter=None, plan_ts=None, last_attempt_messages=None)
    real_agent = Agent(deps_type=type(deps), system_prompt="test")
    run_kwargs = {"user_prompt": "hi", "message_history": None}

    with pytest.raises(RuntimeError) as exc_info:
        agent_mod._run_with_provider_chain(real_agent, run_kwargs, deps)

    message = str(exc_info.value)
    assert "raw HTTP 200 body" in message
    assert "temporarily rate-limited upstream" in message


# ---------------------------------------------------------------------------
# Embeds (whiteboard, HTML, computer_stream_tool) must reply in the current
# thread, not post a new top-level message — send_web_embed's payload had no
# thread_ts at all until now.
# ---------------------------------------------------------------------------


def test_send_web_embed_includes_thread_ts_when_given(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        agent_mod.send_web_embed(
            channel_id="C1", text="t", url="https://example.com", title="title",
            thread_ts="1.2",
        )
    assert post.call_args.kwargs["json"]["thread_ts"] == "1.2"


def test_send_web_embed_omits_thread_ts_when_not_given(monkeypatch):
    """Direct calls without a thread (e.g. from some future non-turn context)
    must not send a bogus empty thread_ts that Slack would reject."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        agent_mod.send_web_embed(channel_id="C1", text="t", url="https://example.com", title="title")
    assert "thread_ts" not in post.call_args.kwargs["json"]


def test_whiteboard_embed_tool_threads_off_the_current_deps_thread_ts(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        agent_mod.send_whiteboard_embed_tool(_run_ctx(Mock()))
    assert post.call_args.kwargs["json"]["thread_ts"] == "1.2"  # _run_ctx's deps.thread_ts


def test_html_embed_tool_threads_off_the_current_deps_thread_ts(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr("agent.web64_client.upload_bytes", lambda *a, **k: "https://example.com/e.html")
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        agent_mod.send_html_embed_tool(_run_ctx(Mock()), html="<p>hi</p>")
    assert post.call_args.kwargs["json"]["thread_ts"] == "1.2"


def test_computer_stream_tool_threads_the_embed_off_the_current_thread(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    monkeypatch.setattr(
        agent_mod, "_computer_stream_start", lambda channel_id, thread_ts: "https://x.e2b.app/vnc.html"
    )
    ctx = _run_ctx(Mock())
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        agent_mod.computer_stream_tool(ctx)
    assert post.call_args.kwargs["json"]["thread_ts"] == "1.2"


def test_computer_stream_tool_marks_keep_sandbox_warm(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    monkeypatch.setattr(
        agent_mod, "_computer_stream_start", lambda channel_id, thread_ts: "https://x.e2b.app/vnc.html"
    )
    ctx = _run_ctx(Mock())
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        agent_mod.computer_stream_tool(ctx)
    assert ctx.deps.keep_sandbox_warm is True


def test_agent_browser_stream_tool_threads_the_embed_and_marks_keep_sandbox_warm(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    monkeypatch.setattr(
        agent_mod, "_agent_browser_stream_start",
        lambda channel_id, thread_ts: "https://x.e2b.app/vnc.html",
    )
    ctx = _run_ctx(Mock())
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        result = agent_mod.agent_browser_stream_tool(ctx)
    assert post.call_args.kwargs["json"]["thread_ts"] == "1.2"
    assert post.call_args.kwargs["json"]["blocks"][0]["video_url"] == "https://x.e2b.app/vnc.html"
    assert ctx.deps.keep_sandbox_warm is True
    # send_web_embed's return value is always a non-empty string (success or
    # error), and computer_stream_tool's identical `if error:` check treats any
    # of them as the failure branch — matching that existing (if surprising)
    # behavior rather than diverging from it for just this tool.
    assert result == "Success: Embed sent to C1 | url: https://x.e2b.app/vnc.html"


def test_agent_browser_stream_tool_requires_e2b_api_key(monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    ctx = _run_ctx(Mock())
    result = agent_mod.agent_browser_stream_tool(ctx)
    assert "E2B_API_KEY" in result


# ---------------------------------------------------------------------------
# run_linux_command must not silently use commands.run()'s own 60s default without
# the model being able to change it — a bare "context deadline exceeded" from the
# SDK is what a user actually hit running a slow agent-browser command this way.
# ---------------------------------------------------------------------------


def _fake_sandbox_recording_timeout(monkeypatch):
    from types import SimpleNamespace

    class _FakeCommands:
        def run(self, cmd, envs=None, timeout=None):
            _FakeCommands.last_call = {"cmd": cmd, "envs": envs, "timeout": timeout}
            return SimpleNamespace(stdout="ok", stderr="", exit_code=0)

    class _FakeSandbox:
        def __init__(self):
            self.commands = _FakeCommands()
            self.paused = 0

        def pause(self):
            self.paused += 1

    fake_sandbox = _FakeSandbox()
    monkeypatch.setattr(agent_mod, "get_or_create_sandbox", lambda c, t: (fake_sandbox, None))
    return _FakeCommands, fake_sandbox


def test_run_linux_command_defaults_to_a_60s_timeout(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    fake_commands, fake_sandbox = _fake_sandbox_recording_timeout(monkeypatch)
    ctx = _run_ctx(Mock())
    agent_mod.run_linux_command(ctx, "echo hi")
    assert fake_commands.last_call["timeout"] == 60


def test_run_linux_command_lets_the_model_raise_the_timeout(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    fake_commands, fake_sandbox = _fake_sandbox_recording_timeout(monkeypatch)
    ctx = _run_ctx(Mock())
    agent_mod.run_linux_command(ctx, "agent-browser open https://en.wikipedia.org/wiki/AI", timeout=1500)
    assert fake_commands.last_call["timeout"] == 1500


def test_run_linux_command_timeout_zero_disables_it(monkeypatch):
    """0 is the model's explicit opt-in to let a command run unbounded — matches
    commands.run()'s own SDK semantics (falsy timeout -> no deadline sent)."""
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    fake_commands, fake_sandbox = _fake_sandbox_recording_timeout(monkeypatch)
    ctx = _run_ctx(Mock())
    agent_mod.run_linux_command(ctx, "echo hi", timeout=0)
    assert fake_commands.last_call["timeout"] == 0


def test_run_linux_command_clamps_an_out_of_range_positive_timeout(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    fake_commands, fake_sandbox = _fake_sandbox_recording_timeout(monkeypatch)
    ctx = _run_ctx(Mock())

    agent_mod.run_linux_command(ctx, "echo hi", timeout=1)
    assert fake_commands.last_call["timeout"] == 10  # floor

    agent_mod.run_linux_command(ctx, "echo hi", timeout=999999)
    assert fake_commands.last_call["timeout"] == 1800  # ceiling


# ---------------------------------------------------------------------------
# Sandbox keepalive (agent.sandbox_keepalive) — a VNC stream needs the sandbox
# to survive between commands, not pause the instant each one returns.
# ---------------------------------------------------------------------------


def test_run_linux_command_pauses_immediately_when_keepalive_is_off(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    fake_commands, fake_sandbox = _fake_sandbox_recording_timeout(monkeypatch)
    armed = []
    monkeypatch.setattr(agent_mod.sandbox_keepalive, "arm", lambda *a: armed.append(a))
    ctx = _run_ctx(Mock())
    ctx.deps.sandbox_keepalive_seconds = 0.0
    agent_mod.run_linux_command(ctx, "echo hi")
    assert fake_sandbox.paused == 1
    assert armed == []


def test_run_linux_command_arms_keepalive_instead_of_pausing_when_active(monkeypatch):
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    fake_commands, fake_sandbox = _fake_sandbox_recording_timeout(monkeypatch)
    armed = []
    monkeypatch.setattr(agent_mod.sandbox_keepalive, "arm", lambda *a: armed.append(a))
    ctx = _run_ctx(Mock())
    ctx.deps.sandbox_keepalive_seconds = 120
    agent_mod.run_linux_command(ctx, "echo hi")
    assert fake_sandbox.paused == 0
    assert armed == [("C1", "1.2", 120)]
    assert ctx.deps.keep_sandbox_warm is True


def test_computer_stream_tool_sets_keepalive_and_arms_it(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    monkeypatch.setattr(agent_mod, "_computer_stream_start", lambda c, t: "https://x.e2b.app/vnc.html")
    armed = []
    monkeypatch.setattr(agent_mod.sandbox_keepalive, "arm", lambda *a: armed.append(a))
    ctx = _run_ctx(Mock())
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        agent_mod.computer_stream_tool(ctx)
    assert ctx.deps.sandbox_keepalive_seconds == 120
    assert armed == [("C1", "1.2", 120)]


def test_agent_browser_stream_tool_sets_keepalive_and_arms_it(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    monkeypatch.setattr(agent_mod, "_agent_browser_stream_start", lambda c, t: "https://x.e2b.app/vnc.html")
    armed = []
    monkeypatch.setattr(agent_mod.sandbox_keepalive, "arm", lambda *a: armed.append(a))
    ctx = _run_ctx(Mock())
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        agent_mod.agent_browser_stream_tool(ctx)
    assert ctx.deps.sandbox_keepalive_seconds == 120
    assert armed == [("C1", "1.2", 120)]


def test_computer_use_action_resets_keepalive_when_active(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    monkeypatch.setattr(agent_mod.provider_config, "is_vision_model", lambda name: True)
    monkeypatch.setattr(agent_mod, "_computer_use_dispatch", lambda *a, **k: "Clicked")
    armed = []
    monkeypatch.setattr(agent_mod.sandbox_keepalive, "arm", lambda *a: armed.append(a))

    from pydantic_ai import RunContext
    deps = SimpleNamespace(
        channel_id="C1", thread_ts="1.2", keep_sandbox_warm=False,
        last_screenshot_post_ts=0.0, sandbox_keepalive_seconds=120,
    )
    ctx = RunContext(model=SimpleNamespace(model_name="anthropic:claude-sonnet-4-6"), usage=None, prompt="", deps=deps)
    agent_mod.computer_use(ctx, action="click", x=1, y=1)
    assert armed == [("C1", "1.2", 120)]


def test_computer_use_action_does_not_touch_keepalive_when_inactive(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    monkeypatch.setattr(agent_mod.provider_config, "is_vision_model", lambda name: True)
    monkeypatch.setattr(agent_mod, "_computer_use_dispatch", lambda *a, **k: "Clicked")
    armed = []
    monkeypatch.setattr(agent_mod.sandbox_keepalive, "arm", lambda *a: armed.append(a))

    from pydantic_ai import RunContext
    deps = SimpleNamespace(
        channel_id="C1", thread_ts="1.2", keep_sandbox_warm=False,
        last_screenshot_post_ts=0.0, sandbox_keepalive_seconds=0.0,
    )
    ctx = RunContext(model=SimpleNamespace(model_name="anthropic:claude-sonnet-4-6"), usage=None, prompt="", deps=deps)
    agent_mod.computer_use(ctx, action="click", x=1, y=1)
    assert armed == []


def test_set_sandbox_keepalive_tool_arms_a_positive_value(monkeypatch):
    armed = []
    monkeypatch.setattr(agent_mod.sandbox_keepalive, "arm", lambda *a: armed.append(a))
    ctx = _run_ctx(Mock())
    result = agent_mod.set_sandbox_keepalive_tool(ctx, 300)
    assert ctx.deps.sandbox_keepalive_seconds == 300
    assert ctx.deps.keep_sandbox_warm is True
    assert armed == [("C1", "1.2", 300)]
    assert "300" in result


def test_set_sandbox_keepalive_tool_clamps_to_the_max():
    ctx = _run_ctx(Mock())
    agent_mod.set_sandbox_keepalive_tool(ctx, 999999)
    assert ctx.deps.sandbox_keepalive_seconds == 1800


def test_set_sandbox_keepalive_tool_zero_cancels(monkeypatch):
    canceled = []
    monkeypatch.setattr(agent_mod.sandbox_keepalive, "cancel", lambda c, t: canceled.append((c, t)))
    ctx = _run_ctx(Mock())
    ctx.deps.sandbox_keepalive_seconds = 120
    result = agent_mod.set_sandbox_keepalive_tool(ctx, 0)
    assert ctx.deps.sandbox_keepalive_seconds == 0
    assert canceled == [("C1", "1.2")]
    assert "disabled" in result.lower()


def test_run_agent_finally_cancels_keepalive_before_pausing():
    """Whatever countdown was pending, a turn ending must always pause the sandbox —
    never leave it running into the next turn on the strength of an old timer."""
    import inspect

    src = inspect.getsource(agent_mod.run_agent)
    finally_block = src[src.index("finally:"):]
    assert "sandbox_keepalive.cancel(" in finally_block
    assert finally_block.index("sandbox_keepalive.cancel(") < finally_block.index(".pause()")


# ---------------------------------------------------------------------------
# agent.tools.agent_browser_stream.agent_browser_stream — must ensure the
# desktop is up before starting its stream (a --headed agent-browser session
# renders into that desktop; if it's not up yet, there's nothing to render into).
# ---------------------------------------------------------------------------


def test_agent_browser_stream_ensures_desktop_before_starting_stream(monkeypatch):
    # agent/tools/__init__.py does `from .agent_browser_stream import agent_browser_stream`,
    # which overwrites the `agent_browser_stream` attribute on the `agent.tools` package with
    # the function itself — `import agent.tools.agent_browser_stream as x` would resolve to
    # that shadowed function, not the submodule. Go through sys.modules directly instead.
    abs_mod = importlib.import_module("agent.tools.agent_browser_stream")

    calls = []
    fake_sandbox = object()
    fake_proxy_info = {"token": "t"}

    monkeypatch.setattr(abs_mod, "get_or_create_sandbox", lambda c, t: (fake_sandbox, fake_proxy_info))
    monkeypatch.setattr(abs_mod.dh, "ensure_desktop", lambda sb, pi: calls.append(("ensure_desktop", sb, pi)))
    monkeypatch.setattr(abs_mod.dh, "start_stream", lambda sb, pi: calls.append(("start_stream", sb, pi)) or "https://x.e2b.app/vnc.html")

    url = abs_mod.agent_browser_stream("C1", "1.1")

    assert url == "https://x.e2b.app/vnc.html"
    assert [c[0] for c in calls] == ["ensure_desktop", "start_stream"]
    assert all(c[1] is fake_sandbox and c[2] is fake_proxy_info for c in calls)


# ---------------------------------------------------------------------------
# _maybe_post_screenshot — throttled screenshot posting to the thread (both
# computer_use and a --headed agent-browser session share this desktop, so
# this is the one hook that makes "frequent-ish screenshots" work for both).
# ---------------------------------------------------------------------------


def test_maybe_post_screenshot_uploads_and_posts_an_image_block(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr("agent.web64_client.upload_bytes", lambda *a, **k: "https://example.com/shot.png")
    ctx = _run_ctx(Mock())
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        agent_mod._maybe_post_screenshot(ctx, b"fake-png-bytes")
    assert post.call_args.kwargs["json"]["channel"] == "C1"
    assert post.call_args.kwargs["json"]["thread_ts"] == "1.2"
    assert post.call_args.kwargs["json"]["blocks"][0] == {
        "type": "image", "image_url": "https://example.com/shot.png", "alt_text": "desktop screenshot",
    }
    assert ctx.deps.last_screenshot_post_ts > 0


def test_maybe_post_screenshot_throttles_rapid_calls(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr("agent.web64_client.upload_bytes", lambda *a, **k: "https://example.com/shot.png")
    ctx = _run_ctx(Mock())
    ctx.deps.last_screenshot_post_ts = time.time()  # "just posted"
    with patch("agent.agent.requests.post") as post:
        agent_mod._maybe_post_screenshot(ctx, b"fake-png-bytes")
    post.assert_not_called()


def test_maybe_post_screenshot_swallows_upload_errors(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    def boom(*a, **k):
        raise RuntimeError("upload failed")

    monkeypatch.setattr("agent.web64_client.upload_bytes", boom)
    ctx = _run_ctx(Mock())
    agent_mod._maybe_post_screenshot(ctx, b"fake-png-bytes")  # must not raise
    assert ctx.deps.last_screenshot_post_ts == 0.0  # not updated on failure


def test_computer_use_screenshot_action_posts_to_thread(monkeypatch):
    from pydantic_ai import RunContext
    from types import SimpleNamespace

    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    monkeypatch.setattr(agent_mod.provider_config, "is_vision_model", lambda name: True)
    monkeypatch.setattr(agent_mod, "_computer_use_dispatch", lambda *a, **k: b"fake-png-bytes")

    posted = []
    monkeypatch.setattr(agent_mod, "_maybe_post_screenshot", lambda ctx, png: posted.append(png))

    deps = SimpleNamespace(
        channel_id="C1", thread_ts="1.2", keep_sandbox_warm=False, last_screenshot_post_ts=0.0,
        sandbox_keepalive_seconds=0.0,
    )
    ctx = RunContext(model=SimpleNamespace(model_name="anthropic:claude-sonnet-4-6"), usage=None, prompt="", deps=deps)
    agent_mod.computer_use(ctx, action="screenshot")
    assert posted == [b"fake-png-bytes"]
