"""run_agent must build a system prompt that stays byte-identical across
turns of the same thread (providers can only cache an exact-match prefix),
and must not silently disable Anthropic's opt-in prompt caching."""

from types import SimpleNamespace
from unittest.mock import Mock

import importlib

import pytest

agent_mod = importlib.import_module("agent.agent")


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


def test_run_agent_sets_one_openai_prompt_cache_key_for_every_thread(captured_runs):
    """The actually-configured production provider (HCAI, an OpenAI-compatible
    proxy) only caches when requests carry a prompt_cache_key (verified live).
    The key is shared by every thread: the system prompt + tools prefix is the
    same everywhere, so a new thread's first turn should land on the worker
    that already has it cached — verified live 2026-09-24: with a shared key,
    a second thread's first turn hit cache for 28,293 of 28,488 tokens, vs 0
    with a per-thread key."""
    from agent.deps import AgentDeps
    thread_a = AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1", message_ts="100.100", platform=FakePlatform())
    thread_b = AgentDeps(client=Mock(), user_id="U2", channel_id="C2", thread_ts="2.2", message_ts="200.200", platform=FakePlatform())

    agent_mod.run_agent("hello", thread_a)
    agent_mod.run_agent("hello", thread_b)

    settings_a = captured_runs[0][1]["model_settings"]
    settings_b = captured_runs[1][1]["model_settings"]
    assert settings_a["openai_prompt_cache_key"] == settings_b["openai_prompt_cache_key"]
    assert settings_a["openai_prompt_cache_retention"] == "24h"


def test_system_prompt_is_identical_across_threads_and_users(captured_runs):
    """Thread/sender-specific context must never be in the system prompt, or
    the shared cached prefix breaks for every thread but one."""
    from agent.deps import AgentDeps
    agent_mod.run_agent("hello", AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1", message_ts="1.0", platform=FakePlatform()))
    agent_mod.run_agent("hello", AgentDeps(client=Mock(), user_id="U2", channel_id="C2", thread_ts="2.2", message_ts="2.0", platform=FakePlatform()))

    system_a = captured_runs[0][0]._system_prompts[0]
    system_b = captured_runs[1][0]._system_prompts[0]
    assert system_a == system_b
    assert "stable context for" not in system_a
    assert "stable context for C1" in captured_runs[0][1]["user_prompt"]
    assert "stable context for C2" in captured_runs[1][1]["user_prompt"]


def test_custom_instructions_go_in_the_user_prompt_not_the_system_prompt(captured_runs, monkeypatch):
    """Custom instructions are per-user, so they'd split the shared cached
    prefix if they lived in the system prompt."""
    monkeypatch.setattr("listeners.actions.instructions_actions.get_user_instructions", lambda uid: "Be extra concise.")
    agent_mod.run_agent("hello", _deps("100.100"))

    assert "Be extra concise." not in captured_runs[0][0]._system_prompts[0]
    assert "Be extra concise." in captured_runs[0][1]["user_prompt"]
