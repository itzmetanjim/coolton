
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


def test_provider_order_groq_adds_groq_entries(monkeypatch, clean_env):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    order = agent_mod._build_provider_order(None)
    names = [name for name, _ in order]
    assert "groq_0" in names
    assert "groq_1" in names
    assert "groq_2" in names
    assert "groq_3" in names


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
        ("OPENROUTER_API_KEY_FALLBACK", "openrouter:stealth/ox-alpha"),
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

    deps = SimpleNamespace(client=client, channel_id="C1", thread_ts="1.2", user_id="U_TEST")
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
        result = agent_mod.slack_api_call(_run_ctx(Mock()), method="chat.postMessage", params={})
    assert "requires a 'channel'" in result
    post.assert_not_called()


def test_slack_api_call_rejects_chat_post_message_without_text(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    with patch("agent.agent.requests.post") as post:
        result = agent_mod.slack_api_call(
            _run_ctx(Mock()), method="chat.postMessage", params={"channel": "C1"}
        )
    assert "requires a 'text'" in result
    post.assert_not_called()


def test_slack_api_call_allows_empty_params_for_other_methods(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        result = agent_mod.slack_api_call(_run_ctx(Mock()), method="auth.test", params={})
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
            _run_ctx(Mock()), method="conversations.info", params={}
        )
    assert "missing_argument" in result
    assert "required" in result
    assert "channel" in result
    assert "provided" in result


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
    deps = SimpleNamespace(user_id=None, provider_tag_filter=None, plan_ts="1.1")

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
    deps = SimpleNamespace(user_id=None, provider_tag_filter=None, plan_ts="1.1")

    result, provider = agent_mod._run_with_provider_chain(fake_agent, {}, deps)

    assert provider == "anthropic"
    assert shown_models == ["hcai_0 / openai/gpt-5.6-luna", "anthropic / anthropic:claude-sonnet-4-6"]
