from types import SimpleNamespace
from unittest.mock import Mock

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

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


def _tokens_msg(tokens: int) -> ModelResponse:
    """A single message whose estimated token size is exactly `tokens` —
    _estimate_tokens is a chars/4 count, so this pads content to match."""
    return ModelResponse(parts=[TextPart(content="x" * (tokens * hc._CHARS_PER_TOKEN))])


def _deps():
    return SimpleNamespace(channel_id="C1", thread_ts="1.1", user_id="U1")


def test_short_history_is_untouched():
    messages = _messages(5)
    result = hc.maybe_compact_history(messages, _deps())
    assert result == messages


def test_long_history_gets_compacted(monkeypatch):
    # 50 messages x 1,000 estimated tokens = 50,000, over the 40,000 threshold.
    messages = [_tokens_msg(1000) for _ in range(50)]
    monkeypatch.setattr(hc, "_summarize", lambda transcript, deps: "dense summary text")

    result = hc.maybe_compact_history(messages, _deps())

    # Tail budget is 12,000 tokens / 1,000 each = 12 messages kept verbatim.
    assert len(result) == 13
    assert result[1:] == messages[-12:]
    summary_msg = result[0]
    assert isinstance(summary_msg, ModelRequest)
    assert "dense summary text" in summary_msg.parts[0].content
    assert "compressed" in summary_msg.parts[0].content


def test_summarizer_failure_keeps_full_history(monkeypatch):
    messages = [_tokens_msg(1000) for _ in range(50)]

    def boom(transcript, deps):
        raise RuntimeError("no provider available")

    monkeypatch.setattr(hc, "_summarize", boom)

    result = hc.maybe_compact_history(messages, _deps())
    assert result == messages


def test_empty_summary_keeps_full_history(monkeypatch):
    messages = [_tokens_msg(1000) for _ in range(50)]
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


# ---------------------------------------------------------------------------
# Token estimation — coarse chars/4, but must actually add up tool call args
# and tool returns, not just plain text, since a large sandbox command/result
# is real context weight too.
# ---------------------------------------------------------------------------


def test_estimate_tokens_counts_text_content():
    messages = [_tokens_msg(500)]
    assert hc._estimate_tokens(messages) == 500


def test_estimate_tokens_counts_tool_call_args():
    msg = ModelResponse(parts=[ToolCallPart(tool_name="t", args="a" * 400, tool_call_id="c1")])
    assert hc._estimate_tokens([msg]) == 100


def test_estimate_tokens_counts_tool_return_content():
    msg = ModelRequest(parts=[ToolReturnPart(tool_name="t", content="a" * 400, tool_call_id="c1")])
    assert hc._estimate_tokens([msg]) == 100


def test_estimate_tokens_sums_across_messages():
    messages = [_tokens_msg(100), _tokens_msg(200), _tokens_msg(300)]
    assert hc._estimate_tokens(messages) == 600


# ---------------------------------------------------------------------------
# _safe_split_index — a token-budget boundary can land between a tool call
# and its return; every provider rejects sending the return without its
# matching call ("No tool call found for function call output with call_id
# ..."), which is exactly what a naive backward token walk could do to a
# thread that happened to be mid-tool-call at that offset.
# ---------------------------------------------------------------------------


def test_safe_split_index_keeps_tail_within_token_budget():
    messages = [_tokens_msg(1000) for _ in range(30)]
    split = hc._safe_split_index(messages, hc.KEEP_TAIL_TOKENS)
    tail = messages[split:]
    assert hc._estimate_tokens(tail) <= hc.KEEP_TAIL_TOKENS
    assert len(tail) == 12  # 12 x 1,000 = 12,000; a 13th would push over budget


def test_safe_split_index_always_keeps_at_least_the_last_message():
    # A single message far bigger than the whole tail budget must still be
    # the tail, rather than producing an empty tail.
    messages = [_tokens_msg(1), _tokens_msg(1), _tokens_msg(50_000)]
    split = hc._safe_split_index(messages, hc.KEEP_TAIL_TOKENS)
    assert split == 2
    assert messages[split:] == [messages[2]]


def test_safe_split_index_pulls_a_split_tool_round_into_the_tail():
    # Sized so the naive token-budget boundary lands exactly on the
    # ToolReturnPart, one message after its ToolCallPart.
    call_tokens = 1
    return_tokens = 5000
    tool_round = [
        ModelResponse(parts=[ToolCallPart(
            tool_name="t", args="a" * (call_tokens * hc._CHARS_PER_TOKEN), tool_call_id="call_1",
        )]),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="t", content="a" * (return_tokens * hc._CHARS_PER_TOKEN), tool_call_id="call_1",
        )]),
    ]
    filler_after = [_tokens_msg(1000) for _ in range(7)]  # 7,000 tokens
    messages = [_tokens_msg(1000) for _ in range(5)] + tool_round + filler_after

    naive_tail_start = len(messages) - len(filler_after) - 1  # index of the ToolReturnPart
    assert messages[naive_tail_start] is tool_round[1]
    # sanity: 7,000 (filler) + 5,000 (return) == the 12,000 budget exactly,
    # and the call right before it would push it over.
    assert hc._estimate_tokens(messages[naive_tail_start:]) == hc.KEEP_TAIL_TOKENS
    assert hc._has_pending_tool_call(messages[naive_tail_start - 1])

    split = hc._safe_split_index(messages, hc.KEEP_TAIL_TOKENS)

    assert split == naive_tail_start - 1  # pulled the call in alongside its return
    assert not hc._has_pending_tool_call(messages[split - 1])


def test_maybe_compact_history_never_orphans_a_tool_return(monkeypatch):
    call_tokens = 1
    return_tokens = 5000
    tool_round = [
        ModelResponse(parts=[ToolCallPart(
            tool_name="t", args="a" * (call_tokens * hc._CHARS_PER_TOKEN), tool_call_id="call_1",
        )]),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="t", content="a" * (return_tokens * hc._CHARS_PER_TOKEN), tool_call_id="call_1",
        )]),
    ]
    filler_after = [_tokens_msg(1000) for _ in range(7)]
    # Padding at the front just to clear the 40,000-token compaction threshold.
    messages = [_tokens_msg(5000) for _ in range(8)] + tool_round + filler_after
    monkeypatch.setattr(hc, "_summarize", lambda transcript, deps: "dense summary text")

    result = hc.maybe_compact_history(messages, _deps())

    tail = result[1:]
    call_ids_with_calls = {
        p.tool_call_id
        for m in tail if isinstance(m, ModelResponse)
        for p in m.parts if isinstance(p, ToolCallPart)
    }
    call_ids_with_returns = {
        p.tool_call_id
        for m in tail if isinstance(m, ModelRequest)
        for p in m.parts if isinstance(p, ToolReturnPart)
    }
    # Every return kept in the tail must have its call kept alongside it —
    # never sent to a provider with the call summarized away.
    assert call_ids_with_returns <= call_ids_with_calls
    assert "call_1" in call_ids_with_returns  # confirms the scenario actually exercised the guard
