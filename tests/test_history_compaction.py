from types import SimpleNamespace
from unittest.mock import Mock

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from agent import history_compaction as hc


def _msg_pair(i: int):
    """One request/response pair, counted as 2 ModelMessages."""
    return [
        ModelRequest(parts=[UserPromptPart(content=f"user message {i}")]),
        ModelResponse(parts=[TextPart(content=f"assistant reply {i}")]),
    ]


def _messages(n_pairs: int) -> list:
    out = []
    for i in range(n_pairs):
        out.extend(_msg_pair(i))
    return out


def _deps():
    return SimpleNamespace(channel_id="C1", thread_ts="1.1", user_id="U1")


def test_short_history_is_untouched():
    messages = _messages(5)
    result = hc.maybe_compact_history(messages, _deps())
    assert result == messages


def test_long_history_gets_compacted(monkeypatch):
    messages = _messages(40)  # 80 ModelMessages, over the 60 threshold
    monkeypatch.setattr(hc, "_summarize", lambda transcript, deps: "dense summary text")

    result = hc.maybe_compact_history(messages, _deps())

    assert len(result) == hc.KEEP_TAIL_MESSAGES + 1
    assert result[1:] == messages[-hc.KEEP_TAIL_MESSAGES:]
    summary_msg = result[0]
    assert isinstance(summary_msg, ModelRequest)
    assert "dense summary text" in summary_msg.parts[0].content
    assert "compressed" in summary_msg.parts[0].content


def test_summarizer_failure_keeps_full_history(monkeypatch):
    messages = _messages(40)

    def boom(transcript, deps):
        raise RuntimeError("no provider available")

    monkeypatch.setattr(hc, "_summarize", boom)

    result = hc.maybe_compact_history(messages, _deps())
    assert result == messages


def test_empty_summary_keeps_full_history(monkeypatch):
    messages = _messages(40)
    monkeypatch.setattr(hc, "_summarize", lambda transcript, deps: "   ")

    result = hc.maybe_compact_history(messages, _deps())
    assert result == messages


def test_summarize_prefers_subagent(monkeypatch):
    run_subagent_mock = Mock(return_value="subagent summary")
    monkeypatch.setattr("agent.subagents.run_subagent", run_subagent_mock)

    result = hc._summarize("some transcript", _deps())

    assert result == "subagent summary"
    run_subagent_mock.assert_called_once()
    assert run_subagent_mock.call_args[0][0] == "summarizer"


def test_summarize_falls_back_to_direct_model_call(monkeypatch):
    monkeypatch.setattr(
        "agent.subagents.run_subagent", Mock(side_effect=RuntimeError("subagent unavailable"))
    )
    fake_response = SimpleNamespace(parts=[TextPart(content="direct fallback summary")])
    monkeypatch.setattr(
        "pydantic_ai.direct.model_request_sync", Mock(return_value=fake_response)
    )
    monkeypatch.setattr("agent.provider_config.get_model_from_config", Mock(return_value="test-model"))

    result = hc._summarize("some transcript", _deps())
    assert result == "direct fallback summary"


def test_render_for_summary_flattens_text_parts():
    messages = _msg_pair(1)
    rendered = hc._render_for_summary(messages)
    assert "user message 1" in rendered
    assert "assistant reply 1" in rendered
