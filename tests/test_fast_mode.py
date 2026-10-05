"""agent.fast_mode: [!FAST] trades the careful research default for a quick answer."""
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.fast_mode import FAST_NOTE, extract_fast_directive

agent_mod = importlib.import_module("agent.agent")


@pytest.mark.parametrize("text,cleaned,fast", [
    ("[!FAST] what is renew", "what is renew", True),
    ("what is renew [!fast]", "what is renew", True),
    ("how do i send \\[!FAST] literally", "how do i send [!FAST] literally", False),
    ("plain message", "plain message", False),
])
def test_the_directive_is_found_and_stripped(text, cleaned, fast):
    assert extract_fast_directive(text) == (cleaned, fast)


@pytest.mark.parametrize("fast", [False, True])
def test_fast_turns_drop_high_reasoning_and_say_so(monkeypatch, fast):
    from tests.test_prompt_caching import FakePlatform

    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr("listeners.actions.instructions_actions.get_user_instructions", lambda uid: "")
    captured = {}
    monkeypatch.setattr(agent_mod, "_run_with_provider_chain", lambda a, kw, deps, run_label=None: (
        captured.update(kw), (SimpleNamespace(output="ok", all_messages=lambda: []), "x"))[1])
    from agent.deps import AgentDeps
    deps = AgentDeps(client=Mock(), user_id="U1", channel_id="C1", thread_ts="1.1", message_ts="1.0",
                     platform=FakePlatform(), fast=fast)

    agent_mod.run_agent("hello", deps)

    assert ("openai_reasoning_effort" not in captured["model_settings"]) is fast
    prompt = captured["user_prompt"] if captured["user_prompt"] is not None else str(captured["message_history"])
    assert (FAST_NOTE.strip()[:40] in prompt) is fast
