import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.plan_block import (
    _combined_io,
    _display_for_tool,
    _messages_safe_for_resume,
    _pretty_args,
    _retrying,
    _rich_output,
    _truncate,
    build_plan_blocks,
    build_plan_hooks,
    build_task_dict,
    complete_plan_message,
    delete_plan_message,
    finalize_plan_message,
    send_plan_message,
    set_model_task,
    set_plan_error,
    update_plan_message,
)


def _deps(**overrides):
    kwargs = {
        "client": Mock(),
        "channel_id": "C1",
        "thread_ts": "1.1",
        "plan_ts": None,
        "plan_tasks": {},
        "model_used": "",
        "run_started_at": 0.0,
    }
    kwargs.update(overrides)
    return SimpleNamespace(**kwargs)


# ---------------------------------------------------------------------------
# Block builders
# ---------------------------------------------------------------------------


def test_build_task_dict_minimal():
    card = build_task_dict("t1", "Title", "in_progress")
    assert card == {"task_id": "t1", "title": "Title", "status": "in_progress"}


def test_build_task_dict_with_details_and_output():
    card = build_task_dict("t1", "Title", "complete", details="doing stuff", output="done")
    assert card["details"]["type"] == "rich_text"
    assert card["output"]["type"] == "rich_text"


def test_build_plan_blocks_structure():
    blocks = build_plan_blocks("Thinking...", [{"task_id": "t1", "title": "x", "status": "in_progress"}])
    assert len(blocks) == 1
    plan = blocks[0]
    assert plan["type"] == "plan"
    assert plan["title"] == "Thinking..."
    assert plan["block_id"].startswith("plan")
    assert plan["tasks"][0]["task_id"] == "t1"


# ---------------------------------------------------------------------------
# send / update / finalize / complete / delete
# ---------------------------------------------------------------------------


def test_send_plan_message_returns_ts():
    deps = _deps()
    deps.client.chat_postMessage.return_value = {"ts": "123.456"}
    assert send_plan_message(deps) == "123.456"
    deps.client.chat_postMessage.assert_called_once()
    assert "task_thinking" in deps.plan_tasks


def test_send_plan_message_returns_none_on_error():
    deps = _deps()
    deps.client.chat_postMessage.side_effect = Exception("network down")
    assert send_plan_message(deps) is None


def test_send_plan_message_no_ts():
    deps = _deps()
    deps.client.chat_postMessage.return_value = {}
    assert send_plan_message(deps) is None


def test_update_plan_message_noop_without_plan_ts():
    deps = _deps()
    update_plan_message(deps)
    deps.client.chat_update.assert_not_called()


def test_update_plan_message_calls_chat_update():
    deps = _deps(plan_ts="100.100", plan_tasks={"task_thinking": {"task_id": "task_thinking", "title": "Thinking", "status": "in_progress"}})
    update_plan_message(deps)
    deps.client.chat_update.assert_called_once()


def test_set_plan_error_marks_tasks_and_clears_plan_ts():
    deps = _deps(plan_ts="100.100", plan_tasks={"task_thinking": {"task_id": "task_thinking", "title": "Thinking", "status": "in_progress"}})
    set_plan_error(deps, "boom")
    assert deps.plan_ts is None
    assert deps.plan_tasks["task_thinking"]["status"] == "complete"
    error_tasks = [t for t in deps.plan_tasks.values() if t["title"] == "Something went wrong"]
    assert len(error_tasks) == 1
    assert error_tasks[0]["status"] == "error"
    deps.client.chat_update.assert_called_once()


def test_set_plan_error_noop_without_plan_ts():
    deps = _deps()
    set_plan_error(deps, "boom")
    deps.client.chat_update.assert_not_called()


def test_finalize_plan_message_adds_responding_and_model():
    deps = _deps(plan_ts="100.100", model_used="anthropic / claude", plan_tasks={"task_thinking": {"task_id": "task_thinking", "title": "Thinking", "status": "in_progress"}})
    finalize_plan_message(deps)
    titles = {t["title"] for t in deps.plan_tasks.values()}
    assert "Responding" in titles
    assert "Model: anthropic / claude" in titles
    assert deps.plan_tasks["task_thinking"]["status"] == "complete"
    responding = next(t for t in deps.plan_tasks.values() if t["title"] == "Responding")
    assert responding["status"] == "in_progress"


def test_finalize_plan_message_shows_model_first():
    """The model choice reads as a header, not a trailing detail — it should
    lead the task list even though it's decided last (after every tool step)."""
    deps = _deps(
        plan_ts="100.100",
        model_used="anthropic / claude",
        plan_tasks={
            "task_thinking": {"task_id": "task_thinking", "title": "Thinking", "status": "in_progress"},
            "task_tool1": {"task_id": "task_tool1", "title": "search_web", "status": "complete"},
        },
    )
    finalize_plan_message(deps)
    ordered_titles = [t["title"] for t in deps.plan_tasks.values()]
    assert ordered_titles[0] == "Model: anthropic / claude"
    assert ordered_titles[1:] == ["Thinking", "search_web", "Responding"]


# ---------------------------------------------------------------------------
# set_model_task — live, per-attempt model display
# ---------------------------------------------------------------------------


def test_set_model_task_noop_without_plan_ts():
    deps = _deps(plan_ts=None)
    set_model_task(deps, "hcai / gpt-5.6-luna")
    assert deps.plan_tasks == {}
    deps.client.chat_update.assert_not_called()


def test_set_model_task_shows_model_immediately_first():
    deps = _deps(plan_ts="100.100", plan_tasks={"task_thinking": {"task_id": "task_thinking", "title": "Thinking", "status": "in_progress"}})
    set_model_task(deps, "hcai / gpt-5.6-luna")
    ordered_titles = [t["title"] for t in deps.plan_tasks.values()]
    assert ordered_titles[0] == "Model: hcai / gpt-5.6-luna"
    assert deps.plan_tasks["task_model"]["status"] == "in_progress"
    deps.client.chat_update.assert_called_once()


def test_set_model_task_updates_in_place_on_fallback_instead_of_stacking():
    deps = _deps(plan_ts="100.100", plan_tasks={})
    set_model_task(deps, "hcai / gpt-5.6-luna")
    set_model_task(deps, "openrouter_fb / glm-5.2")
    titles = [t["title"] for t in deps.plan_tasks.values()]
    assert titles == ["Model: openrouter_fb / glm-5.2"]


def test_finalize_plan_message_does_not_duplicate_a_live_model_task():
    """When set_model_task already ran (the normal path once a provider is
    picked), finalize_plan_message must reuse that task, not add a second
    "Model:" line."""
    deps = _deps(plan_ts="100.100", model_used="hcai / gpt-5.6-luna", plan_tasks={})
    set_model_task(deps, "hcai / gpt-5.6-luna")
    finalize_plan_message(deps)
    model_titles = [t["title"] for t in deps.plan_tasks.values() if t["title"].startswith("Model:")]
    assert model_titles == ["Model: hcai / gpt-5.6-luna"]
    assert deps.plan_tasks["task_model"]["status"] == "complete"


def test_complete_plan_message_completes_responding():
    deps = _deps(plan_ts="100.100", plan_tasks={"task_1": {"task_id": "task_1", "title": "Doing", "status": "in_progress"}})
    complete_plan_message(deps)
    assert deps.plan_tasks["task_1"]["status"] == "complete"
    deps.client.chat_update.assert_called_once()


def test_delete_plan_message():
    deps = _deps(plan_ts="100.100")
    delete_plan_message(deps)
    deps.client.chat_delete.assert_called_once_with(channel="C1", ts="100.100")
    assert deps.plan_ts is None


def test_delete_plan_message_noop_without_plan_ts():
    deps = _deps()
    delete_plan_message(deps)
    deps.client.chat_delete.assert_not_called()


# ---------------------------------------------------------------------------
# _retrying — terminal plan-block transitions must not strand the spinner on
# a transient Slack failure (chat_update rate-limited by the burst of live
# updates a busy run produces, a network blip, etc).
# ---------------------------------------------------------------------------


def test_retrying_succeeds_first_try_without_sleeping(monkeypatch):
    import agent.plan_block as pb
    sleep = Mock()
    monkeypatch.setattr(pb.time, "sleep", sleep)

    call = Mock()
    _retrying(call, "somewhere")

    call.assert_called_once()
    sleep.assert_not_called()


def test_retrying_recovers_after_a_transient_failure(monkeypatch):
    import agent.plan_block as pb
    monkeypatch.setattr(pb.time, "sleep", Mock())

    call = Mock(side_effect=[RuntimeError("boom"), None])
    _retrying(call, "somewhere")

    assert call.call_count == 2


def test_retrying_gives_up_after_exhausting_attempts_without_raising(monkeypatch):
    import agent.plan_block as pb
    monkeypatch.setattr(pb.time, "sleep", Mock())

    call = Mock(side_effect=RuntimeError("boom"))
    _retrying(call, "somewhere", attempts=3)

    assert call.call_count == 3


def test_retrying_respects_retry_after_header(monkeypatch):
    import agent.plan_block as pb
    sleep = Mock()
    monkeypatch.setattr(pb.time, "sleep", sleep)

    err = RuntimeError("rate limited")
    err.response = SimpleNamespace(headers={"Retry-After": "2"})
    call = Mock(side_effect=[err, None])
    _retrying(call, "somewhere")

    sleep.assert_called_once_with(2.0)


def test_set_plan_error_retries_chat_update_on_failure(monkeypatch):
    monkeypatch.setattr("agent.plan_block.time.sleep", Mock())
    deps = _deps(plan_ts="100.100")
    deps.client.chat_update.side_effect = [RuntimeError("boom"), None]

    set_plan_error(deps, "oh no")

    assert deps.client.chat_update.call_count == 2
    assert deps.plan_ts is None


def test_complete_plan_message_retries_chat_update_on_failure(monkeypatch):
    monkeypatch.setattr("agent.plan_block.time.sleep", Mock())
    deps = _deps(plan_ts="100.100")
    deps.client.chat_update.side_effect = [RuntimeError("boom"), None]

    complete_plan_message(deps)

    assert deps.client.chat_update.call_count == 2


def test_delete_plan_message_retries_chat_delete_on_failure(monkeypatch):
    monkeypatch.setattr("agent.plan_block.time.sleep", Mock())
    deps = _deps(plan_ts="100.100")
    deps.client.chat_delete.side_effect = [RuntimeError("boom"), None]

    delete_plan_message(deps)

    assert deps.client.chat_delete.call_count == 2
    assert deps.plan_ts is None


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def _hook(hooks, name):
    return hooks._registry[name][0].func


def test_build_plan_hooks_tracks_tool_lifecycle():
    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100")
    ctx = SimpleNamespace(deps=deps, messages=[])
    call = SimpleNamespace(tool_name="fetch_url_tool", tool_call_id="abc123")
    args = {"url": "https://example.com"}

    async def run():
        await _hook(hooks, "before_tool_execute")(
            ctx, call=call, tool_def=None, args=args
        )
        await _hook(hooks, "after_tool_execute")(
            ctx, call=call, tool_def=None, args=args, result="fetched"
        )

    asyncio.run(run())

    task = deps.plan_tasks["task_abc123"]
    assert task["status"] == "complete"
    assert "fetched" in task["output"]["elements"][0]["elements"][0]["text"]


def test_build_plan_hooks_after_tool_summarizes_binary_content_in_plan_card():
    """The plan-card output (task["output"], built via _combined_io) is a second,
    separate str(result) call site from _pretty_args's own logging call — both
    needed the BinaryContent fix, not just one."""
    from pydantic_ai.messages import ToolReturn, BinaryContent

    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100")
    ctx = SimpleNamespace(deps=deps, messages=[])
    call = SimpleNamespace(tool_name="computer_use", tool_call_id="shot1")
    result = ToolReturn(
        return_value="Screenshot of your desktop.",
        content=["(Desktop screenshot via computer_use)", BinaryContent(data=b"x" * 200_000, media_type="image/png")],
    )

    async def run():
        await _hook(hooks, "before_tool_execute")(ctx, call=call, tool_def=None, args={"action": "screenshot"})
        await _hook(hooks, "after_tool_execute")(ctx, call=call, tool_def=None, args={}, result=result)

    asyncio.run(run())

    task = deps.plan_tasks["task_shot1"]
    output_text = task["output"]["elements"][0]["elements"][0]["text"]
    assert len(output_text) < 1000
    assert "image/png" in output_text
    assert "x" * 100 not in output_text


def test_build_plan_hooks_folds_steering_message_into_next_tool_result():
    from agent.steering_store import clear_steering_messages, queue_steering_message

    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100", channel_id="STEER1", thread_ts="1.1")
    ctx = SimpleNamespace(deps=deps, messages=[])
    call = SimpleNamespace(tool_name="fetch_url_tool", tool_call_id="abc123")
    queue_steering_message("STEER1", "1.1", "also check the other thing", "U9")

    async def run():
        return await _hook(hooks, "after_tool_execute")(
            ctx, call=call, tool_def=None, args={}, result="fetched"
        )

    try:
        result = asyncio.run(run())
    finally:
        clear_steering_messages("STEER1", "1.1")

    assert "fetched" in result
    assert "also check the other thing" in result


def test_build_plan_hooks_clears_steering_queue_once_delivered():
    from agent.steering_store import (
        clear_steering_messages,
        peek_steering_messages,
        queue_steering_message,
    )

    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100", channel_id="STEER2", thread_ts="1.1")
    ctx = SimpleNamespace(deps=deps, messages=[])
    call = SimpleNamespace(tool_name="fetch_url_tool", tool_call_id="abc123")
    queue_steering_message("STEER2", "1.1", "hey", "U9")

    async def run():
        await _hook(hooks, "after_tool_execute")(
            ctx, call=call, tool_def=None, args={}, result="fetched"
        )

    try:
        asyncio.run(run())
        assert peek_steering_messages("STEER2", "1.1") == []
    finally:
        clear_steering_messages("STEER2", "1.1")


def test_build_plan_hooks_leaves_steering_queued_when_result_is_not_a_string():
    from agent.steering_store import (
        clear_steering_messages,
        peek_steering_messages,
        queue_steering_message,
    )

    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100", channel_id="STEER3", thread_ts="1.1")
    ctx = SimpleNamespace(deps=deps, messages=[])
    call = SimpleNamespace(tool_name="some_tool", tool_call_id="abc123")
    queue_steering_message("STEER3", "1.1", "hey", "U9")

    async def run():
        return await _hook(hooks, "after_tool_execute")(
            ctx, call=call, tool_def=None, args={}, result={"structured": True}
        )

    try:
        result = asyncio.run(run())
        assert result == {"structured": True}
        assert len(peek_steering_messages("STEER3", "1.1")) == 1
    finally:
        clear_steering_messages("STEER3", "1.1")


def test_build_plan_hooks_no_steering_queued_leaves_result_untouched():
    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100", channel_id="STEER4", thread_ts="1.1")
    ctx = SimpleNamespace(deps=deps, messages=[])
    call = SimpleNamespace(tool_name="fetch_url_tool", tool_call_id="abc123")

    async def run():
        return await _hook(hooks, "after_tool_execute")(
            ctx, call=call, tool_def=None, args={}, result="fetched"
        )

    result = asyncio.run(run())
    assert result == "fetched"


def test_build_plan_hooks_does_not_track_when_plan_ts_unset():
    hooks = build_plan_hooks()
    deps = _deps()  # plan_ts None
    ctx = SimpleNamespace(deps=deps, messages=[])
    call = SimpleNamespace(tool_name="fetch_url_tool", tool_call_id="abc123")

    async def run():
        await _hook(hooks, "before_tool_execute")(
            ctx, call=call, tool_def=None, args={"url": "x"}
        )

    asyncio.run(run())
    assert deps.plan_tasks == {}


def test_build_plan_hooks_updates_live_thread_status_on_tool_call():
    import agent.thread_status as thread_status

    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100", channel_id="TS1", thread_ts="1.1")
    thread_status.start(deps.client, "TS1", "1.1")
    deps.client.assistant_threads_setStatus.reset_mock()
    ctx = SimpleNamespace(deps=deps, messages=[])
    call = SimpleNamespace(tool_name="add_emoji_reaction", tool_call_id="abc123")

    async def run():
        await _hook(hooks, "before_tool_execute")(ctx, call=call, tool_def=None, args={})

    try:
        asyncio.run(run())
        deps.client.assistant_threads_setStatus.assert_called_once_with(
            channel_id="TS1", thread_ts="1.1", status="calling tool: Reacting to message"
        )
    finally:
        thread_status.stop("TS1", "1.1")


def test_build_plan_hooks_updates_live_thread_status_even_without_a_plan_message():
    """The live status pill is independent of the plan/thinking block — it should still
    update even when plan_ts is unset (e.g. send_plan_message failed)."""
    import agent.thread_status as thread_status

    hooks = build_plan_hooks()
    deps = _deps(channel_id="TS2", thread_ts="1.1")  # plan_ts None
    thread_status.start(deps.client, "TS2", "1.1")
    deps.client.assistant_threads_setStatus.reset_mock()
    ctx = SimpleNamespace(deps=deps, messages=[])
    call = SimpleNamespace(tool_name="search_web_tool", tool_call_id="abc123")

    async def run():
        await _hook(hooks, "before_tool_execute")(ctx, call=call, tool_def=None, args={})

    try:
        asyncio.run(run())
        deps.client.assistant_threads_setStatus.assert_called_once_with(
            channel_id="TS2", thread_ts="1.1", status="calling tool: Searching the web"
        )
    finally:
        thread_status.stop("TS2", "1.1")


def test_build_plan_hooks_tool_error_marks_error_and_reraises():
    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100")
    ctx = SimpleNamespace(deps=deps, messages=[])
    call = SimpleNamespace(tool_name="fetch_url_tool", tool_call_id="abc123")

    async def run():
        await _hook(hooks, "before_tool_execute")(
            ctx, call=call, tool_def=None, args={"url": "x"}
        )
        with pytest.raises(RuntimeError, match="nope"):
            await _hook(hooks, "on_tool_execute_error")(
                ctx, call=call, tool_def=None, args={}, error=RuntimeError("nope")
            )

    asyncio.run(run())
    assert deps.plan_tasks["task_abc123"]["status"] == "error"


def test_before_tool_hook_snapshots_messages_and_halts_on_stop(monkeypatch):
    from agent.stop_store import HaltRun
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart

    monkeypatch.setattr("agent.plan_block.stop_requested_for", lambda *a, **k: True)
    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100", run_started_at=0.0)
    messages = [
        ModelRequest(parts=[UserPromptPart(content="do the thing")]),
        ModelResponse(parts=[ToolCallPart(tool_name="fetch_url_tool", args={})]),
    ]
    ctx = SimpleNamespace(deps=deps, messages=messages)
    call = SimpleNamespace(tool_name="fetch_url_tool", tool_call_id="abc123")

    async def run():
        await _hook(hooks, "before_tool_execute")(ctx, call=call, tool_def=None, args={})

    with pytest.raises(HaltRun, match="!stop"):
        asyncio.run(run())

    # The dangling tool call (no matching ToolReturnPart) is dropped; the
    # user's message that started this turn is kept.
    assert deps.halted_messages == messages[:1]


def test_before_tool_hook_checkpoints_progress_on_every_call_not_just_stop():
    """deps.last_attempt_messages must update on every tool call, not only when !stop is
    requested — agent.agent._run_with_provider_chain relies on this to resume a mid-turn
    provider fallback from where the turn actually got to, instead of restarting it."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart

    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100", run_started_at=0.0)
    messages = [
        ModelRequest(parts=[UserPromptPart(content="do the thing")]),
        ModelResponse(parts=[ToolCallPart(tool_name="fetch_url_tool", args={})]),
    ]
    ctx = SimpleNamespace(deps=deps, messages=messages)
    call = SimpleNamespace(tool_name="fetch_url_tool", tool_call_id="abc123")

    async def run():
        await _hook(hooks, "before_tool_execute")(ctx, call=call, tool_def=None, args={})

    asyncio.run(run())

    assert deps.last_attempt_messages == messages[:1]


def test_messages_safe_for_resume_drops_trailing_pending_tool_call():
    from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, UserPromptPart

    messages = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(parts=[ToolCallPart(tool_name="t", args={})]),
    ]
    assert _messages_safe_for_resume(messages) == messages[:1]


def test_messages_safe_for_resume_keeps_completed_history_untouched():
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    messages = [
        ModelRequest(parts=[UserPromptPart(content="hi")]),
        ModelResponse(parts=[TextPart(content="done")]),
    ]
    assert _messages_safe_for_resume(messages) == messages


def test_messages_safe_for_resume_handles_empty_list():
    assert _messages_safe_for_resume([]) == []


def test_build_plan_hooks_shows_reasoning_between_tool_calls():
    from pydantic_ai.messages import ModelResponse, ThinkingPart, ToolCallPart

    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100", plan_tasks={"task_thinking": {"task_id": "task_thinking", "title": "Thinking", "status": "in_progress"}})
    ctx = SimpleNamespace(deps=deps, messages=[])
    response = ModelResponse(parts=[
        ThinkingPart(content="I should search the web for this."),
        ToolCallPart(tool_name="search_web_tool", args={"query": "x"}),
    ])

    async def run():
        return await _hook(hooks, "after_model_request")(ctx, request_context=None, response=response)

    result = asyncio.run(run())

    assert result is response
    assert "task_thinking" not in deps.plan_tasks
    reasoning_tasks = [t for t in deps.plan_tasks.values() if t["title"] == "Reasoning"]
    assert len(reasoning_tasks) == 1
    task = reasoning_tasks[0]
    assert task["status"] == "complete"
    assert "I should search the web for this." in task["output"]["elements"][0]["elements"][0]["text"]


def test_build_plan_hooks_ignores_response_with_no_thinking_part():
    from pydantic_ai.messages import ModelResponse, TextPart

    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100")
    ctx = SimpleNamespace(deps=deps, messages=[])
    response = ModelResponse(parts=[TextPart(content="just a normal answer")])

    async def run():
        await _hook(hooks, "after_model_request")(ctx, request_context=None, response=response)

    asyncio.run(run())
    assert deps.plan_tasks == {}


def test_build_plan_hooks_reasoning_noop_when_plan_ts_unset():
    from pydantic_ai.messages import ModelResponse, ThinkingPart

    hooks = build_plan_hooks()
    deps = _deps()  # plan_ts None
    ctx = SimpleNamespace(deps=deps, messages=[])
    response = ModelResponse(parts=[ThinkingPart(content="secret reasoning")])

    async def run():
        await _hook(hooks, "after_model_request")(ctx, request_context=None, response=response)

    asyncio.run(run())
    assert deps.plan_tasks == {}


def test_build_plan_hooks_posts_status_update_alongside_tool_call():
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100")
    ctx = SimpleNamespace(deps=deps, messages=[])
    response = ModelResponse(parts=[
        TextPart(content="→ _checking the deploy logs for the last restart_"),
        ToolCallPart(tool_name="search_web_tool", args={"query": "x"}),
    ])

    async def run():
        return await _hook(hooks, "after_model_request")(ctx, request_context=None, response=response)

    result = asyncio.run(run())

    assert result is response
    deps.client.chat_postMessage.assert_called_once()
    kwargs = deps.client.chat_postMessage.call_args.kwargs
    assert kwargs["channel"] == "C1"
    assert kwargs["thread_ts"] == "1.1"
    assert kwargs["markdown_text"] == "→ _checking the deploy logs for the last restart_"


def test_build_plan_hooks_does_not_repost_final_answer_as_status():
    """A response with no tool calls is the final answer, already posted by
    run_agent_turn — must not be duplicated here."""
    from pydantic_ai.messages import ModelResponse, TextPart

    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100")
    ctx = SimpleNamespace(deps=deps, messages=[])
    response = ModelResponse(parts=[TextPart(content="here's the final answer")])

    async def run():
        await _hook(hooks, "after_model_request")(ctx, request_context=None, response=response)

    asyncio.run(run())
    deps.client.chat_postMessage.assert_not_called()


def test_build_plan_hooks_skips_status_post_when_tool_call_has_no_text():
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100")
    ctx = SimpleNamespace(deps=deps, messages=[])
    response = ModelResponse(parts=[ToolCallPart(tool_name="t", args={})])

    async def run():
        await _hook(hooks, "after_model_request")(ctx, request_context=None, response=response)

    asyncio.run(run())
    deps.client.chat_postMessage.assert_not_called()


def test_build_plan_hooks_status_post_survives_slack_error():
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart

    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100")
    deps.client.chat_postMessage.side_effect = RuntimeError("boom")
    ctx = SimpleNamespace(deps=deps, messages=[])
    response = ModelResponse(parts=[
        TextPart(content="→ _trying again_"),
        ToolCallPart(tool_name="t", args={}),
    ])

    async def run():
        return await _hook(hooks, "after_model_request")(ctx, request_context=None, response=response)

    result = asyncio.run(run())
    assert result is response


def test_build_plan_hooks_multiple_reasoning_rounds_do_not_overwrite():
    """Each model round's reasoning gets its own task — the point is to show the
    trace between every tool call, not just the latest one."""
    from pydantic_ai.messages import ModelResponse, ThinkingPart, ToolCallPart

    hooks = build_plan_hooks()
    deps = _deps(plan_ts="100.100")
    ctx = SimpleNamespace(deps=deps, messages=[])

    async def run():
        await _hook(hooks, "after_model_request")(
            ctx, request_context=None,
            response=ModelResponse(parts=[ThinkingPart(content="round one"), ToolCallPart(tool_name="t", args={})]),
        )
        await _hook(hooks, "after_model_request")(
            ctx, request_context=None,
            response=ModelResponse(parts=[ThinkingPart(content="round two")]),
        )

    asyncio.run(run())
    reasoning_titles = [t for t in deps.plan_tasks.values() if t["title"] == "Reasoning"]
    assert len(reasoning_titles) == 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def test_display_for_tool_known():
    assert _display_for_tool("fetch_url_tool") == "Fetching URL"


def test_display_for_tool_mcp():
    assert _display_for_tool("mcp_server:mail_search") == "Slack: mail_search"


def test_display_for_tool_unknown():
    assert _display_for_tool("do_thing") == "Do thing"


def test_pretty_args_variants():
    assert _pretty_args({"a": 1, "b": "x"}) == "{a=1, b=x}"
    assert _pretty_args([1, "two"]) == "[1, two]"
    assert _pretty_args("plain") == "plain"


def test_pretty_args_summarizes_binary_content_instead_of_stringifying_it():
    """A computer_use screenshot's ToolReturn embeds a BinaryContent with the raw
    PNG bytes. str()-ing that (the old behavior) builds a ~megabyte escaped string
    per screenshot before any truncation happens — this must stay a short summary
    regardless of payload size."""
    from pydantic_ai.messages import ToolReturn, BinaryContent

    big_image = BinaryContent(data=b"x" * 200_000, media_type="image/png")
    result = ToolReturn(
        return_value="Screenshot of your desktop.",
        content=["(Desktop screenshot via computer_use)", big_image],
    )
    rendered = _pretty_args(result)
    assert len(rendered) < 500
    assert "Screenshot of your desktop." in rendered
    assert "image/png" in rendered
    assert "200000 bytes" in rendered
    assert "x" * 100 not in rendered  # the raw byte payload never got embedded


def test_pretty_args_binary_content_directly():
    from pydantic_ai.messages import BinaryContent

    img = BinaryContent(data=b"\x89PNG\x00" * 10_000, media_type="image/png")
    assert _pretty_args(img) == f"<image/png, {len(img.data)} bytes>"


def test_truncate():
    assert _truncate("short") == "short"
    assert len(_truncate("x" * 500, max_len=200)) == 200
    assert _truncate("", 200) == "Done"
    assert _truncate(None, 200) == "Done"


def test_rich_output_object():
    out = _rich_output("hello world", 200)
    assert out["type"] == "rich_text"


def test_combined_io():
    combined = _combined_io("ls -la", "3 files")
    text = combined["elements"][0]["elements"][0]["text"]
    assert "$ ls -la" in text
    assert "3 files" in text
