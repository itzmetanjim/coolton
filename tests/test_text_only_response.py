"""text_only_response is an output function: one model call that both reacts and
ends the turn with the reply as result.output (which run_agent_turn then posts
like any plain-text final answer)."""
import importlib
from types import SimpleNamespace

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


def _run(model_fn, monkeypatch):
    surface = _Surface()
    monkeypatch.setattr("agent.tools.emoji_reaction._surface", lambda deps: surface)
    monkeypatch.setattr("agent.tools.emoji_reaction.random.random", lambda: 1.0)  # never randomly skip
    calls = []

    def counting(messages, info):
        calls.append(info)
        return model_fn(messages, info)

    test_agent = Agent(FunctionModel(counting), deps_type=SimpleNamespace, output_type=agent_mod.OUTPUT_TYPE)
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
