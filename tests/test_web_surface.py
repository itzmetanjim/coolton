"""agent.surfaces.web.WebSurface — every method should append exactly one
structured event of the expected shape into the conversation's log, since
that's the only thing the frontend has to go on."""

from types import SimpleNamespace

import pytest

from agent.surfaces.web import WebSurface
from web import conversation_log as log


@pytest.fixture(autouse=True)
def tmp_store(monkeypatch, tmp_path):
    monkeypatch.setattr(log, "STORE_DIR", str(tmp_path / "web_conversations"))
    log._locks.clear()
    log._last_seq.clear()
    with log._subscribers_guard:
        log._subscribers.clear()
    return tmp_path


@pytest.fixture
def conversation_id():
    return log.create_conversation("U1")


def _events(conversation_id):
    return log.read_events(conversation_id)


def test_post_text_appends_a_status_variant_agent_message(conversation_id):
    WebSurface(conversation_id, "U1").post_text("working on it")
    ev = _events(conversation_id)[0]
    assert ev["type"] == "agent_message"
    assert ev["variant"] == "status"
    assert ev["text"] == "working on it"


def test_post_final_appends_a_final_variant_agent_message(conversation_id):
    WebSurface(conversation_id, "U1").post_final("here's the answer")
    ev = _events(conversation_id)[0]
    assert ev["variant"] == "final"
    assert ev["text"] == "here's the answer"


def test_post_error_appends_an_error_turn_end(conversation_id):
    WebSurface(conversation_id, "U1").post_error("something broke")
    ev = _events(conversation_id)[0]
    assert ev["type"] == "turn_end"
    assert ev["state"] == "error"
    assert ev["reason"] == "something broke"


def test_react_targets_the_message_set_by_set_target_message(conversation_id):
    surface = WebSurface(conversation_id, "U1")
    surface.set_target_message(7)
    surface.react("tada")
    ev = _events(conversation_id)[0]
    assert ev["type"] == "reaction"
    assert ev["op"] == "add"
    assert ev["emoji"] == "tada"
    assert ev["target_seq"] == 7


def test_remove_reaction_appends_a_remove_op(conversation_id):
    surface = WebSurface(conversation_id, "U1")
    surface.set_target_message(3)
    surface.remove_reaction("tada")
    ev = _events(conversation_id)[0]
    assert ev["op"] == "remove"


def test_set_engaged_is_reported_as_not_applicable_and_writes_nothing(conversation_id):
    result = WebSurface(conversation_id, "U1").set_engaged(True)
    assert "not applicable" in result.lower()
    assert _events(conversation_id) == []


def test_set_model_appends_a_model_step(conversation_id):
    surface = WebSurface(conversation_id, "U1")
    surface.set_model(SimpleNamespace(), "anthropic / claude-opus-5")
    ev = _events(conversation_id)[0]
    assert ev["type"] == "step"
    assert ev["kind"] == "model"
    assert ev["text"] == "anthropic / claude-opus-5"


def test_finish_turn_reports_complete_on_a_normal_finish(conversation_id):
    surface = WebSurface(conversation_id, "U1")
    surface.finish_turn(SimpleNamespace(should_skip=False))
    ev = _events(conversation_id)[0]
    assert ev["type"] == "turn_end"
    assert ev["state"] == "complete"


def test_finish_turn_reports_stopped_on_a_stop_halt(conversation_id):
    surface = WebSurface(conversation_id, "U1")
    surface.finish_turn(SimpleNamespace(should_skip=True, halt_reason="!stop requested"))
    ev = _events(conversation_id)[0]
    assert ev["state"] == "stopped"


def test_finish_turn_reports_skipped_on_a_plain_skip(conversation_id):
    surface = WebSurface(conversation_id, "U1")
    surface.finish_turn(SimpleNamespace(should_skip=True, halt_reason="skip"))
    ev = _events(conversation_id)[0]
    assert ev["state"] == "skipped"


def test_post_image_appends_an_image_variant(conversation_id):
    result = WebSurface(conversation_id, "U1").post_image("https://x/img.png", "a screenshot")
    assert result is None
    ev = _events(conversation_id)[0]
    assert ev["variant"] == "image"
    assert ev["url"] == "https://x/img.png"
    assert ev["alt_text"] == "a screenshot"


def test_post_embed_appends_an_embed_variant(conversation_id):
    WebSurface(conversation_id, "U1").post_embed("https://x/e", "title", "text", "https://x/thumb")
    ev = _events(conversation_id)[0]
    assert ev["variant"] == "embed"
    assert ev["title"] == "title"
    assert ev["thumbnail_url"] == "https://x/thumb"


def test_build_hooks_returns_a_hooks_capability(conversation_id):
    surface = WebSurface(conversation_id, "U1")
    hooks = surface.build_hooks(SimpleNamespace())
    assert hooks is not None


# --- whiteboard/HTML embed tools on the web surface ------------------------
# These used to call send_web_embed (a raw Slack chat.postMessage) directly,
# bypassing Surface entirely — on the web UI that meant a Slack API error
# ("channel_not_found" for channel_id="web") and no embed event at all, not
# just a plain-link fallback. They were switched to go through
# Surface.post_embed the same way computer_stream_tool already did; these
# prove the web path actually produces an embed event now.


def _web_run_ctx(conversation_id):
    from pydantic_ai import RunContext

    surface = WebSurface(conversation_id, "U1")
    deps = SimpleNamespace(channel_id="web", thread_ts=conversation_id, surface=surface)
    return RunContext(model=None, usage=None, prompt="", deps=deps)


def test_whiteboard_embed_tool_posts_an_embed_event_on_the_web_surface(conversation_id):
    import importlib
    agent_mod = importlib.import_module("agent.agent")

    ctx = _web_run_ctx(conversation_id)
    result = agent_mod.send_whiteboard_embed_tool(ctx, whiteboard_id="ABC123")

    assert "whiteboard id: ABC123" in result
    ev = _events(conversation_id)[0]
    assert ev["type"] == "agent_message"
    assert ev["variant"] == "embed"
    assert ev["url"] == "https://whiteboard.felix.hackclub.app/ABC123"


def test_html_embed_tool_posts_an_embed_event_on_the_web_surface(conversation_id, monkeypatch):
    import importlib
    agent_mod = importlib.import_module("agent.agent")

    monkeypatch.setattr("agent.web64_client.upload_bytes", lambda *a, **k: "https://example.com/embed.html")
    ctx = _web_run_ctx(conversation_id)
    result = agent_mod.send_html_embed_tool(ctx, html="<p>hi</p>")

    assert result.startswith("Success")
    ev = _events(conversation_id)[0]
    assert ev["variant"] == "embed"
    assert ev["url"] == "https://example.com/embed.html"


# --- the web-only stop checkpoint -----------------------------------------
# Slack checks !stop in before_tool_execute alone, so a turn that has stopped
# calling tools and is writing its answer ignores it. The web UI's Stop button
# gets an extra checkpoint in after_model_request; these cover that it halts
# when a stop is pending and stays out of the way when one isn't.


def _after_model_hook(conversation_id):
    from agent.surfaces.web_hooks import build_web_hooks
    return build_web_hooks(conversation_id).after_model_request


def _model_ctx(channel_id, thread_ts, run_started_at):
    deps = SimpleNamespace(
        channel_id=channel_id, thread_ts=thread_ts, run_started_at=run_started_at,
        last_attempt_messages=None, halted_messages=None,
    )
    return SimpleNamespace(deps=deps, messages=[])


def test_after_model_halts_when_a_stop_was_requested(conversation_id):
    import asyncio
    from agent.stop_store import HaltRun, request_stop

    ctx = _model_ctx("web", conversation_id, run_started_at=1.0)
    request_stop("web", conversation_id)  # now() > 1.0, so it applies to this run
    handler = _after_model_hook(conversation_id)

    with pytest.raises(HaltRun):
        asyncio.run(handler(ctx, request_context=None, response=SimpleNamespace(parts=[])))


def test_after_model_ignores_a_stop_from_before_this_run_started(conversation_id):
    import asyncio
    import time as _time

    from agent.stop_store import request_stop

    request_stop("web", conversation_id)
    # This run started after that stop, so the stop must not touch it.
    ctx = _model_ctx("web", conversation_id, run_started_at=_time.time() + 60)
    handler = _after_model_hook(conversation_id)

    response = SimpleNamespace(parts=[])
    assert asyncio.run(handler(ctx, request_context=None, response=response)) is response
