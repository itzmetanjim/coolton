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


def _deps(**overrides):
    kwargs = {"channel_id": "C1", "thread_ts": "1.1", "user_id": "U1", "provider_tag_filter": None}
    kwargs.update(overrides)
    return SimpleNamespace(**kwargs)


def _patch_context_window(monkeypatch, window: int):
    """maybe_compact_history derives its budget from
    agent.provider_config.get_min_context_window (a local import, so it must
    be patched at its source, not on the hc module)."""
    mock = Mock(return_value=window)
    monkeypatch.setattr("agent.provider_config.get_min_context_window", mock)
    return mock


def test_short_history_is_untouched(monkeypatch):
    _patch_context_window(monkeypatch, 128_000)
    messages = _messages(5)
    result = hc.maybe_compact_history(messages, _deps())
    assert result == messages


def test_long_history_gets_compacted(monkeypatch):
    window = 128_000
    _patch_context_window(monkeypatch, window)
    threshold, keep_tail_tokens = hc._compaction_budget(window)
    n_messages = (threshold // 1000) + 5  # comfortably over threshold
    messages = [_tokens_msg(1000) for _ in range(n_messages)]
    monkeypatch.setattr(hc, "_summarize", lambda transcript, deps: "dense summary text")

    result = hc.maybe_compact_history(messages, _deps())

    expected_tail_count = keep_tail_tokens // 1000
    assert len(result) == expected_tail_count + 1
    assert result[1:] == messages[-expected_tail_count:]
    summary_msg = result[0]
    assert isinstance(summary_msg, ModelRequest)
    assert "dense summary text" in summary_msg.parts[0].content
    assert "compressed" in summary_msg.parts[0].content


def test_summarizer_failure_keeps_full_history(monkeypatch):
    window = 128_000
    _patch_context_window(monkeypatch, window)
    threshold, _ = hc._compaction_budget(window)
    messages = [_tokens_msg(1000) for _ in range((threshold // 1000) + 5)]

    def boom(transcript, deps):
        raise RuntimeError("no provider available")

    monkeypatch.setattr(hc, "_summarize", boom)

    result = hc.maybe_compact_history(messages, _deps())
    assert result == messages


def test_empty_summary_keeps_full_history(monkeypatch):
    window = 128_000
    _patch_context_window(monkeypatch, window)
    threshold, _ = hc._compaction_budget(window)
    messages = [_tokens_msg(1000) for _ in range((threshold // 1000) + 5)]
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
# _compaction_budget — the trigger threshold and tail size scale off the
# smallest reachable model's context window (see get_min_context_window in
# provider_config.py), not a fixed constant. providers.json has models
# ranging from ~131K to 1M+ tokens; sizing this for the small end would waste
# most of a 1M-token model's headroom, sizing it for the large end would
# risk overflowing a 131K one.
# ---------------------------------------------------------------------------


def test_compaction_budget_scales_with_context_window():
    small_threshold, small_tail = hc._compaction_budget(128_000)
    large_threshold, large_tail = hc._compaction_budget(1_048_576)
    assert large_threshold > small_threshold
    assert large_tail > small_tail


def test_compaction_budget_applies_fractions():
    threshold, tail = hc._compaction_budget(200_000)
    assert threshold == int(200_000 * hc._COMPACTION_TRIGGER_FRACTION)
    assert tail == int(200_000 * hc._KEEP_TAIL_FRACTION)


def test_compaction_budget_floors_a_tiny_context_window():
    threshold, tail = hc._compaction_budget(1_000)
    assert threshold == hc._MIN_COMPACTION_TOKEN_THRESHOLD
    assert tail == hc._MIN_KEEP_TAIL_TOKENS


def test_maybe_compact_history_triggers_earlier_for_a_smaller_context_window(monkeypatch):
    """The same history must compact under a small-context model's chain but
    not under a large-context one — proof the threshold is actually dynamic,
    not still a fixed constant in disguise."""
    monkeypatch.setattr(hc, "_summarize", lambda transcript, deps: "dense summary text")
    small_threshold, _ = hc._compaction_budget(131_072)
    large_threshold, _ = hc._compaction_budget(1_048_576)
    assert small_threshold < large_threshold  # sanity: the scenario below actually distinguishes them

    n_tokens_total = small_threshold + 5_000  # over the small budget, comfortably under the large one
    messages = [_tokens_msg(1000) for _ in range(n_tokens_total // 1000)]

    _patch_context_window(monkeypatch, 131_072)
    assert hc.maybe_compact_history(messages, _deps()) != messages

    _patch_context_window(monkeypatch, 1_048_576)
    assert hc.maybe_compact_history(messages, _deps()) == messages


def test_maybe_compact_history_passes_the_tag_filter_through(monkeypatch):
    mock = _patch_context_window(monkeypatch, 128_000)
    hc.maybe_compact_history(_messages(2), _deps(provider_tag_filter="luna"))
    mock.assert_called_once_with("luna")


# ---------------------------------------------------------------------------
# _safe_split_index — a token-budget boundary can land between a tool call
# and its return; every provider rejects sending the return without its
# matching call ("No tool call found for function call output with call_id
# ..."), which is exactly what a naive backward token walk could do to a
# thread that happened to be mid-tool-call at that offset. This is a pure
# function of an explicit token budget — independent of context_window
# lookups, so no patching needed here.
# ---------------------------------------------------------------------------

_TEST_KEEP_TAIL = 12_000


def test_safe_split_index_keeps_tail_within_token_budget():
    messages = [_tokens_msg(1000) for _ in range(30)]
    split = hc._safe_split_index(messages, _TEST_KEEP_TAIL)
    tail = messages[split:]
    assert hc._estimate_tokens(tail) <= _TEST_KEEP_TAIL
    assert len(tail) == 12  # 12 x 1,000 = 12,000; a 13th would push over budget


def test_safe_split_index_always_keeps_at_least_the_last_message():
    # A single message far bigger than the whole tail budget must still be
    # the tail, rather than producing an empty tail.
    messages = [_tokens_msg(1), _tokens_msg(1), _tokens_msg(50_000)]
    split = hc._safe_split_index(messages, _TEST_KEEP_TAIL)
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
    assert hc._estimate_tokens(messages[naive_tail_start:]) == _TEST_KEEP_TAIL
    assert hc._has_pending_tool_call(messages[naive_tail_start - 1])

    split = hc._safe_split_index(messages, _TEST_KEEP_TAIL)

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
    window = 128_000
    threshold, _ = hc._compaction_budget(window)
    # Padding at the front just to clear the compaction threshold.
    head_padding = [_tokens_msg(1000) for _ in range((threshold // 1000) + 1)]
    messages = head_padding + tool_round + filler_after
    _patch_context_window(monkeypatch, window)
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
