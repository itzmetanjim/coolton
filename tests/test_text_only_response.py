"""text_only_response is an output function: one model call that both reacts and
ends the turn with the reply as result.output (which run_agent_turn then posts
like any plain-text final answer)."""
import importlib
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

agent_mod = importlib.import_module("agent.agent")


class _Surface:
    def __init__(self):
        self.reactions = []

    def react(self, emoji_name):
        self.reactions.append(emoji_name)
        return f"Reacted with :{emoji_name}:"


def _run(model_fn, monkeypatch, tools=()):
    surface = _Surface()
    monkeypatch.setattr("agent.tools.emoji_reaction._surface", lambda deps: surface)
    monkeypatch.setattr("agent.tools.emoji_reaction.random.random", lambda: 1.0)  # never randomly skip
    calls = []

    def counting(messages, info):
        calls.append(info)
        return model_fn(messages, info)

    test_agent = Agent(FunctionModel(counting), deps_type=SimpleNamespace, output_type=agent_mod.OUTPUT_TYPE, tools=tools)
    result = test_agent.run_sync("hi", deps=SimpleNamespace())
    return result, surface, calls


def test_reacts_and_ends_the_turn_in_a_single_model_call(monkeypatch):
    def model(messages, info):
        return ModelResponse(parts=[ToolCallPart("text_only_response", {"emoji_name": "wave", "response": "hey!"})])

    result, surface, calls = _run(model, monkeypatch)
    assert result.output == "hey!"
    assert surface.reactions == ["wave"]
    assert len(calls) == 1  # no second round trip to the model


def test_plain_text_is_still_a_valid_final_answer(monkeypatch):
    result, surface, _ = _run(lambda messages, info: ModelResponse(parts=[TextPart("plain answer")]), monkeypatch)
    assert result.output == "plain answer"
    assert surface.reactions == []


@pytest.mark.parametrize("written", [
    'text_only_response(emoji_name="tada", response="all set, it\'s done.\\n\\nbye")',
    # Seen live: raw line breaks inside an ordinary string, so it isn't valid Python.
    'text_only_response(emoji_name="tada", response="all set, it\'s done.\n\nbye")',
    # Seen live: a triple-quoted response, then leaked tool-call markup after the call.
    'text_only_response(\nemoji_name="tada",\nresponse="""all set, it\'s done.\n\nbye""")\n</parameter>\n</invoke>.',
])
def test_a_text_only_response_written_out_as_text_still_only_sends_the_reply(monkeypatch, written):
    """Seen live: replies of `text_only_response(emoji_name="tada", response="...")` as
    plain text, which were posted parameters and all."""
    result, surface, calls = _run(lambda messages, info: ModelResponse(parts=[TextPart(written)]), monkeypatch)

    assert result.output == "all set, it's done.\n\nbye" and surface.reactions == ["tada"]
    assert len(calls) == 1


def test_a_failed_reaction_still_sends_the_reply(monkeypatch):
    class Broken(_Surface):
        def react(self, emoji_name):
            raise RuntimeError("slack down")

    monkeypatch.setattr("agent.tools.emoji_reaction._surface", lambda deps: Broken())
    test_agent = Agent(
        FunctionModel(lambda m, i: ModelResponse(parts=[ToolCallPart("text_only_response", {"emoji_name": "x", "response": "still here"})])),
        deps_type=SimpleNamespace, output_type=agent_mod.OUTPUT_TYPE,
    )
    assert test_agent.run_sync("hi", deps=SimpleNamespace()).output == "still here"


def test_a_turn_reacts_to_its_message_only_once(monkeypatch):
    """Seen live: a long turn reacted first, again ~35 steps later (the model forgot),
    and once more when it ended with text_only_response: three reactions on one message."""
    from agent.tools.emoji_reaction import add_emoji_reaction

    steps = iter([
        ToolCallPart("add_emoji_reaction", {"emoji_name": "brain"}),
        ToolCallPart("add_emoji_reaction", {"emoji_name": "trophy"}),
        ToolCallPart("text_only_response", {"emoji_name": "medal", "response": "done"}),
    ])
    result, surface, _ = _run(lambda messages, info: ModelResponse(parts=[next(steps)]), monkeypatch,
                              tools=[add_emoji_reaction])
    assert result.output == "done"
    assert surface.reactions == ["brain"]


def test_text_beside_a_text_only_response_call_is_not_posted_as_a_status_update(monkeypatch):
    """Text beside a normal tool call is mid-turn narration, posted right away; beside
    text_only_response it's the model talking about its reply (seen live: "text_only_response
    can't add a reaction and a reply in one step here, so I'll just reply.")."""
    from unittest.mock import Mock

    from agent.plan_block import build_plan_hooks

    monkeypatch.setattr("agent.tools.emoji_reaction._surface", lambda deps: _Surface())
    monkeypatch.setattr("agent.tools.emoji_reaction.random.random", lambda: 1.0)
    deps = SimpleNamespace(client=Mock(), channel_id="C1", thread_ts="1.1", plan_ts=None)
    test_agent = Agent(
        FunctionModel(lambda m, i: ModelResponse(parts=[
            TextPart("text_only_response can't do both, so I'll just reply."),
            ToolCallPart("text_only_response", {"emoji_name": "tada", "response": "done"})])),
        deps_type=SimpleNamespace, output_type=agent_mod.OUTPUT_TYPE, capabilities=[build_plan_hooks()],
    )

    assert test_agent.run_sync("hi", deps=deps).output == "done"
    deps.client.chat_postMessage.assert_not_called()
