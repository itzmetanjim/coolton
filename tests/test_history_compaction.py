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


# ---------------------------------------------------------------------------
# _safe_split_index — a fixed message-count boundary can land between a tool
# call and its return; every provider rejects sending the return without its
# matching call ("No tool call found for function call output with call_id
# ..."), which is exactly what a naive `messages[-KEEP_TAIL_MESSAGES:]` slice
# could do to a thread that happened to be mid-tool-call at that offset.
# ---------------------------------------------------------------------------


def _tool_round(call_id: str):
    """One tool call/return pair, as it actually appears in real history:
    a ModelResponse with the call, then a ModelRequest with the return."""
    return [
        ModelResponse(parts=[ToolCallPart(tool_name="run_linux_command", args={}, tool_call_id=call_id)]),
        ModelRequest(parts=[ToolReturnPart(tool_name="run_linux_command", content="ok", tool_call_id=call_id)]),
    ]


def _filler(n: int):
    """n single-message filler entries (unlike _messages(), 1 message each —
    lets a test land the tool_round at an exact index without parity games)."""
    return [ModelResponse(parts=[TextPart(content=f"filler {i}")]) for i in range(n)]


def _messages_with_tool_round_at_boundary():
    """A history built so the naive `len - KEEP_TAIL_MESSAGES` boundary falls
    exactly between a ToolCallPart and its ToolReturnPart."""
    call_index = hc.COMPACTION_MESSAGE_THRESHOLD
    messages = _filler(call_index) + _tool_round("call_1") + _filler(hc.KEEP_TAIL_MESSAGES - 1)
    naive_split = len(messages) - hc.KEEP_TAIL_MESSAGES
    assert naive_split - 1 == call_index  # sanity-check the construction itself
    assert hc._has_pending_tool_call(messages[naive_split - 1])
    return messages


def test_safe_split_index_matches_naive_split_when_boundary_is_clean():
    messages = _messages(40)
    assert hc._safe_split_index(messages, hc.KEEP_TAIL_MESSAGES) == len(messages) - hc.KEEP_TAIL_MESSAGES


def test_safe_split_index_pulls_a_split_tool_round_into_the_tail():
    messages = _messages_with_tool_round_at_boundary()
    naive_split = len(messages) - hc.KEEP_TAIL_MESSAGES

    split = hc._safe_split_index(messages, hc.KEEP_TAIL_MESSAGES)

    assert split == naive_split - 1
    assert not hc._has_pending_tool_call(messages[split - 1])


def test_maybe_compact_history_never_orphans_a_tool_return(monkeypatch):
    messages = _messages_with_tool_round_at_boundary()
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
