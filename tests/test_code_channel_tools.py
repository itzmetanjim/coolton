"""agent.tools.code_channel_tools and the code channels app's listeners: coolton's tabs,
context bar, canvas, commands and archiving in a code channel, and how people's commands,
button presses and stop clicks there reach her."""
from unittest.mock import Mock

import pytest

from agent import thread_status
from agent.tools import code_channel_tools as tools
from listeners import code_channel_app


@pytest.fixture
def slack(monkeypatch):
    """Records code channel API calls; `responses` maps a method to what it returns."""
    calls, responses = [], {}

    def call(method, **params):
        calls.append((method, params))
        return responses.get(method, {"ok": True})

    canvases, views = {}, {}
    monkeypatch.setattr(tools, "call", call)
    monkeypatch.setattr(tools, "is_code_channel", lambda c: c == "CCODE")
    monkeypatch.setattr(tools, "canvas_views", lambda c: dict(canvases))
    monkeypatch.setattr(tools, "remember_canvas_view",
                        lambda c, key, canvas_id, view_id, name="": canvases.update({key: {"canvas_id": canvas_id, "view_id": view_id}}))
    monkeypatch.setattr(tools, "stored_views", lambda c: dict(views))
    monkeypatch.setattr(tools, "remember_view", lambda c, key, kind, view_id, file_id, name="": views.update(
        {key: {"type": kind, "view_id": view_id, "file_id": file_id, "name": name}}))
    monkeypatch.setattr(tools, "forget_view", lambda c, key: views.pop(key, None))
    return calls, responses


def test_tools_only_work_in_a_code_channel(slack):
    calls, _ = slack
    assert tools.set_view("CNOT", "html", "page", content="<html></html>").startswith("Error")
    assert calls == []


def test_an_html_tab_is_an_upsert_by_view_key_with_its_allowed_origins(slack):
    calls, responses = slack
    responses["agents.conversations.setView"] = {"ok": True, "view_id": "Ct1", "content_version": 1}

    result = tools.set_view("CCODE", "html", "reports/coverage.html", "Coverage", "<!doctype html>",
                            resource_domains="https://cdn.jsdelivr.net")

    assert "Ct1" in result
    assert calls == [("agents.conversations.setView", {
        "channel_id": "CCODE", "type": "html", "name": "Coverage", "view_key": "reports/coverage.html",
        "content": "<!doctype html>", "csp": {"resource_domains": ["https://cdn.jsdelivr.net"]}})]


def test_tabs_are_sent_to_slack_one_at_a_time_per_channel(slack, monkeypatch):
    """Slack fails tab creations that race in one channel (view_creation_failed), and
    coolton makes several tabs with parallel tool calls."""
    import threading
    import time

    active, overlaps = [], []

    def call(method, **params):
        active.append(1)
        overlaps.append(len(active))
        time.sleep(0.05)
        active.pop()
        return {"ok": True, "view_id": params["view_key"]}

    monkeypatch.setattr(tools, "call", call)
    threads = [threading.Thread(target=tools.set_view, args=("CCODE", "html", f"t{i}", "", "<html></html>"))
               for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlaps == [1, 1, 1]


def test_a_new_canvas_tab_is_created_then_later_updated_in_place(slack):
    """Slack keeps no view_key for canvas tabs (and listViews leaves them out), so
    coolton remembers which canvas a key is to update it rather than add a second tab."""
    calls, responses = slack
    responses["canvases.create"] = {"ok": True, "canvas_id": "F1"}
    responses["agents.conversations.setView"] = {"ok": True, "view_id": "Ct1"}

    tools.set_view("CCODE", "canvas", "plan", "Plan", markdown="# Plan")
    assert [m for m, _ in calls] == ["canvases.create", "agents.conversations.setView"]
    assert calls[-1][1]["canvas_id"] == "F1" and calls[-1][1]["access_level"] == "comment"

    calls.clear()
    tools.set_view("CCODE", "canvas", "plan", markdown="# Plan v2")
    assert calls[-1] == ("agents.conversations.setCanvasContent",
                         {"channel": "CCODE", "canvas_id": "F1", "content": "# Plan v2"})


def test_a_block_kit_tab_is_listed_and_readable_though_slack_leaves_it_out(slack, monkeypatch):
    """listViews omits Block Kit (and canvas) tabs; each tab's content is a Slack file."""
    _, responses = slack
    responses["agents.conversations.setView"] = {"ok": True, "view_id": "Ct9", "file_id": "F9"}
    responses["agents.conversations.listViews"] = {"ok": True, "views": []}
    tools.set_view("CCODE", "block_kit", "actions", "Actions", blocks='[{"type": "divider"}]')
    monkeypatch.setattr(tools, "_download", lambda client, file_id: f"content of {file_id}")

    assert "Actions (block_kit): view_key=actions, view_id=Ct9" in tools.list_views("CCODE")
    assert tools.read_view(Mock(), "CCODE", view_id="Ct9") == "content of F9"
    assert tools.remove_view("CCODE", view_key="actions") == "Tab removed."
    assert tools.list_views("CCODE").startswith("No tabs listed.")


def test_reading_a_canvas_includes_its_comments(slack):
    _, responses = slack
    tools.remember_canvas_view("CCODE", "plan", "F1", "Ct1")
    responses["agents.conversations.getCanvas"] = {"ok": True, "title": "Plan", "content": "1. Inventory", "comments": [
        {"user_id": "U1", "quoted_text": "1. Inventory", "text": "billing last?", "replies": [{"user_id": "U2", "text": "+1"}]}]}

    text = tools.read_canvas("CCODE", "plan")

    assert "1. Inventory" in text and "<@U1>" in text and "billing last?" in text and "+1" in text


@pytest.mark.parametrize("items,ok", [
    ('[{"key": "repo", "label": "a/b", "icon": "folder", "url": "https://github.com/a/b"}]', True),
    ('[{"key": "repo"}]', False),
    ('[{"key": "repo", "label": "x", "icon": "rocket"}]', False),
    ("not json", False),
])
def test_context_bar_items_are_checked(slack, items, ok):
    assert (tools.set_context_bar("CCODE", items) == "Context bar updated.") is ok


def test_slash_commands_lose_a_leading_slash_and_bad_names_are_refused(slack):
    calls, _ = slack
    tools.set_commands("CCODE", '[{"name": "/run-tests", "description": "Run the tests"}]')
    assert calls[-1][1]["commands"] == [{"name": "run-tests", "description": "Run the tests"}]
    assert tools.set_commands("CCODE", '[{"name": "Run Tests", "description": "x"}]').startswith("Error")


def test_archiving_posts_the_summary_and_archives_without_it_when_there_is_no_origin(slack, monkeypatch):
    calls, responses = slack
    unregistered = []
    monkeypatch.setattr("agent.code_channel_store.unregister_code_channel", unregistered.append)
    responses["agents.conversations.archive"] = {"ok": False, "error": "no_origin_link"}
    client = Mock()
    client.chat_postMessage.return_value = {"ts": "9.9"}

    tools.archive(client, "CCODE", "done: shipped the fix")

    client.chat_postMessage.assert_called_once_with(channel="CCODE", markdown_text="done: shipped the fix")
    assert [p for _, p in calls] == [{"channel_id": "CCODE", "summary_message_ts": "9.9"}, {"channel_id": "CCODE"}]


def test_a_code_channel_session_is_set_by_the_code_channels_app(monkeypatch):
    sent = []
    monkeypatch.setattr("agent.code_channel_api.call", lambda method, **p: sent.append((method, p)) or {"ok": True})
    client = Mock()

    thread_status._call(client, "agents.sessions.setStatus", "CCODE", "", status="processing")

    assert sent == [("agents.sessions.setStatus", {"channel_id": "CCODE", "status": "processing"})]
    client.api_call.assert_not_called()


def test_a_slash_command_or_button_reaches_coolton_as_that_persons_message(monkeypatch):
    handed = []
    monkeypatch.setattr(code_channel_app, "handle_code_channel_app_mention", lambda event, a, c: handed.append(event))
    coolton = Mock()
    coolton.chat_postMessage.return_value = {"ts": "5.5"}

    code_channel_app.handle_command({"command": "/run-tests", "text": "auth", "channel_id": "CCODE",
                                     "user_id": "U1"}, Mock(), coolton)
    code_channel_app.handle_block_action({"channel": {"id": "CCODE"}, "user": {"id": "U2"}, "actions": [
        {"type": "button", "action_id": "approve", "text": {"text": "Approve"}, "value": "yes"}]}, Mock(), coolton)

    assert coolton.chat_postMessage.call_args_list[0].kwargs["text"] == "<@U1> ran `/run-tests auth`"
    assert [(e["user"], e["text"], e["ts"]) for e in handed] == [
        ("U1", "/run-tests auth", "5.5"),
        ("U2", '[used button "Approve" (value: yes) [action_id: approve] in one of your Block Kit tabs]', "5.5")]


def test_the_stop_button_in_a_code_channel_stops_coolton_there(monkeypatch):
    stopped = []
    monkeypatch.setattr("agent.active_runs.is_run_active", lambda c, t: True)
    monkeypatch.setattr("agent.stop_store.request_stop", lambda c, t: stopped.append((c, t)))

    code_channel_app.handle_session_stopped({"channel": "CCODE"}, Mock())

    assert stopped == [("CCODE", "")]
