"""run_agent must build a system prompt that stays byte-identical across
turns of the same thread (providers can only cache an exact-match prefix),
and must not silently disable Anthropic's opt-in prompt caching."""

import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock

import importlib

import pytest

agent_mod = importlib.import_module("agent.agent")

# pydantic_ai_skills is installed on the deploy target but not declared in
# requirements.txt (see accompanying fix) and isn't present in every dev/test
# venv; run_agent() imports it unconditionally, so stub it here rather than
# skip these tests.
if "pydantic_ai_skills" not in sys.modules:
    _stub = types.ModuleType("pydantic_ai_skills")
    _stub.SkillsCapability = lambda **kwargs: Mock()
    sys.modules["pydantic_ai_skills"] = _stub


class FakePlatform:
    name = "fake"
    system_prompt = "STATIC SYSTEM PROMPT"

    def format_user_message(self, text, deps):
        return text

    def build_context_prompt(self, deps):
        return f"\nstable context for {deps.channel_id}\n"

    def build_turn_context(self, deps, model, is_vision):
        return f"[turn: {deps.message_ts}]\n\n"

    def toolsets(self, deps):
        return []


@pytest.fixture
def clean_env(monkeypatch):
    for key in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "HCAI_API_KEY", "GROQ_API_KEY",
        "GOOGLE_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY_FALLBACK", "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def captured_runs(monkeypatch, clean_env):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr("listeners.actions.instructions_actions.get_user_instructions", lambda uid: "")

    captured = []

    def fake_run_with_provider_chain(agent_dynamic, run_kwargs, deps):
        captured.append((agent_dynamic, run_kwargs))
        return SimpleNamespace(output="ok", all_messages=lambda: []), "anthropic"

    monkeypatch.setattr(agent_mod, "_run_with_provider_chain", fake_run_with_provider_chain)
    return captured


def _deps(message_ts: str):
    from agent.deps import AgentDeps
    return AgentDeps(
        client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1",
        message_ts=message_ts, platform=FakePlatform(),
    )


def test_system_prompt_is_byte_identical_across_turns_with_different_message_ts(captured_runs):
    agent_mod.run_agent("hello", _deps("100.100"))
    agent_mod.run_agent("hello", _deps("200.200"))

    assert len(captured_runs) == 2
    system_prompt_1 = captured_runs[0][0]._system_prompts[0]
    system_prompt_2 = captured_runs[1][0]._system_prompts[0]

    assert system_prompt_1 == system_prompt_2
    assert "100.100" not in system_prompt_1
    assert "200.200" not in system_prompt_2


def test_message_ts_and_model_appear_in_user_prompt_instead(captured_runs):
    agent_mod.run_agent("hello", _deps("100.100"))
    agent_mod.run_agent("hello", _deps("200.200"))

    user_prompt_1 = captured_runs[0][1]["user_prompt"]
    user_prompt_2 = captured_runs[1][1]["user_prompt"]

    assert "100.100" in user_prompt_1
    assert "200.200" in user_prompt_2
    assert user_prompt_1 != user_prompt_2


def test_run_agent_enables_anthropic_prompt_caching(captured_runs):
    agent_mod.run_agent("hello", _deps("100.100"))

    settings = captured_runs[0][1]["model_settings"]
    assert settings["anthropic_cache_instructions"] is True
    assert settings["anthropic_cache_tool_definitions"] is True
    assert settings["anthropic_cache"] is True


def test_run_agent_sets_a_stable_openai_prompt_cache_key(captured_runs):
    """The actually-configured production provider (HCAI, an OpenAI-compatible
    proxy) does NOT auto-cache on a matching prefix alone — verified live: an
    identical system prompt sent twice with no cache key showed cached_tokens=0
    both times; with a stable prompt_cache_key, the second call hit cache for
    ~90% of prompt tokens. Every turn of the SAME thread must reuse the same key."""
    agent_mod.run_agent("hello", _deps("100.100"))
    agent_mod.run_agent("hello", _deps("200.200"))

    settings_1 = captured_runs[0][1]["model_settings"]
    settings_2 = captured_runs[1][1]["model_settings"]

    assert settings_1["openai_prompt_cache_key"] == settings_2["openai_prompt_cache_key"]
    assert "C1" in settings_1["openai_prompt_cache_key"]
    assert "1.1" in settings_1["openai_prompt_cache_key"]
    assert settings_1["openai_prompt_cache_retention"] == "24h"


def test_run_agent_prompt_cache_key_differs_across_threads(captured_runs):
    from agent.deps import AgentDeps
    deps_thread_a = AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1", message_ts="100.100", platform=FakePlatform())
    deps_thread_b = AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="2.2", message_ts="100.100", platform=FakePlatform())

    agent_mod.run_agent("hello", deps_thread_a)
    agent_mod.run_agent("hello", deps_thread_b)

    key_a = captured_runs[0][1]["model_settings"]["openai_prompt_cache_key"]
    key_b = captured_runs[1][1]["model_settings"]["openai_prompt_cache_key"]
    assert key_a != key_b


def test_custom_instructions_still_change_the_system_prompt(captured_runs, monkeypatch):
    """Custom instructions are per-user, not per-turn — they SHOULD vary the
    cached prefix (once) when a user sets/changes them, unlike message_ts."""
    monkeypatch.setattr("listeners.actions.instructions_actions.get_user_instructions", lambda uid: "Be extra concise.")
    agent_mod.run_agent("hello", _deps("100.100"))

    system_prompt = captured_runs[0][0]._system_prompts[0]
    assert "Be extra concise." in system_prompt
