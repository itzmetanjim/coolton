"""!stop must not wipe the thread's conversation history.

run_agent() catches HaltRun (raised from plan_block.before_tool when a !stop
was requested) and used to always fall back to the pre-turn message_history,
discarding the user's message and any tool round-trips already completed this
turn. It now uses deps.halted_messages — a snapshot taken right before the
halt — when the halt came from !stop specifically.
"""

import sys
import types
from unittest.mock import Mock

import importlib

import pytest

agent_mod = importlib.import_module("agent.agent")

if "pydantic_ai_skills" not in sys.modules:
    _stub = types.ModuleType("pydantic_ai_skills")
    _stub.SkillsCapability = lambda **kwargs: Mock()
    _stub.CallableSkillScriptExecutor = lambda **kwargs: Mock()
    _stub.SkillsDirectory = lambda **kwargs: Mock()
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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr("listeners.actions.instructions_actions.get_user_instructions", lambda uid: "")


def _deps(**overrides):
    from agent.deps import AgentDeps
    kwargs = dict(
        client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1",
        message_ts="100.100", platform=FakePlatform(),
    )
    kwargs.update(overrides)
    return AgentDeps(**kwargs)


def test_stop_halt_keeps_the_snapshot_taken_at_halt_time(clean_env, monkeypatch):
    from agent.stop_store import HaltRun

    snapshot = ["msg1", "msg2"]

    def fake_run_with_provider_chain(agent_dynamic, run_kwargs, deps):
        deps.halted_messages = snapshot
        raise HaltRun("!stop requested")

    monkeypatch.setattr(agent_mod, "_run_with_provider_chain", fake_run_with_provider_chain)

    deps = _deps()
    result = agent_mod.run_agent("hello", deps, message_history=["old_history"])

    assert deps.should_skip is True
    assert deps.halt_reason == "!stop requested"
    assert result.all_messages() == snapshot


def test_skip_halt_still_reverts_to_pre_turn_history(clean_env, monkeypatch):
    """skip() has zero side effects by design — unlike !stop, it never sets
    halted_messages, so the turn should still fall back to whatever history
    was passed in."""
    from agent.stop_store import HaltRun

    def fake_run_with_provider_chain(agent_dynamic, run_kwargs, deps):
        raise HaltRun("skip")

    monkeypatch.setattr(agent_mod, "_run_with_provider_chain", fake_run_with_provider_chain)

    deps = _deps()
    result = agent_mod.run_agent("hello", deps, message_history=["old_history"])

    assert deps.should_skip is True
    assert result.all_messages() == ["old_history"]
