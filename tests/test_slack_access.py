"""agent.slack_access and agent.slack_mcp_guard: which Slack reads and API
calls coolton makes on someone's behalf. cooltonUser/the bot can see more than
the person asking, so reads outside the current conversation are limited to
public channels, and the generic API tools only reach allowlisted methods."""
import asyncio
import importlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from pydantic_ai import RunContext

from agent import slack_access
from agent.slack_access import check_api_call, file_info_read_error
from agent.slack_mcp_guard import GuardedSlackMCPToolset, guard_args

ASKER = "U0ASKER1"
agent_mod = importlib.import_module("agent.agent")


@pytest.fixture
def public_only(monkeypatch):
    """Channels starting with P are public; everything else is private."""
    def fake_assert(channel_id, current):
        if channel_id == current or channel_id.startswith("P"):
            return None
        return "Reading DMs, private channels, or external conversations is not allowed."

    monkeypatch.setattr(slack_access, "assert_readable_channel", fake_assert)


# ---------------------------------------------------------------------------
# check_api_call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", [
    "auth.revoke", "admin.users.remove", "conversations.kick", "conversations.archive",
    "conversations.rename", "conversations.invite", "usergroups.update", "users.profile.set",
    "files.upload", "chat.startStream", "canvases.create", "search.messages", "apps.manifest.create",
])
def test_methods_off_the_allowlist_are_refused(method):
    assert "not on coolton's Slack API allowlist" in check_api_call(method, {}, "C1")


def test_allowlisted_post_goes_through():
    assert check_api_call("chat.postMessage", {"channel": "C_ANYWHERE", "text": "hi"}, "C1") is None


def test_a_smuggled_token_is_refused():
    assert "token" in check_api_call("users.info", {"user": "U1", "token": "xoxp-other"}, "C1")


def test_reading_a_private_channel_is_refused(public_only):
    assert "refused" in check_api_call("conversations.history", {"channel": "C_PRIVATE"}, "C1")


def test_reading_a_public_or_the_current_channel_is_allowed(public_only):
    assert check_api_call("conversations.history", {"channel": "P_PUBLIC"}, "C1") is None
    assert check_api_call("conversations.replies", {"channel": "C1", "ts": "1.1"}, "C1") is None


def test_bookmarks_list_uses_channel_id_and_is_checked(public_only):
    assert "refused" in check_api_call("bookmarks.list", {"channel_id": "C_PRIVATE"}, "C1")


def test_channel_reads_need_a_channel():
    assert "channel id is required" in check_api_call("conversations.history", {}, "C1")


def test_reading_a_dm_by_user_id_is_refused():
    assert "not allowed" in check_api_call("conversations.history", {"channel": "U0SOMEONE"}, "C1")


def test_deleting_messages_is_not_allowed_even_in_the_current_conversation():
    assert "allowlist" in check_api_call("chat.delete", {"channel": "C1", "ts": "1.1"}, "C1")


def test_conversations_list_is_public_channels_only():
    assert check_api_call("conversations.list", {}, "C1") is None
    assert check_api_call("conversations.list", {"types": "public_channel"}, "C1") is None
    assert "public channels" in check_api_call("conversations.list", {"types": "public_channel,im"}, "C1")


def test_slack_api_call_tool_refuses_before_calling_slack(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    deps = SimpleNamespace(client=Mock(), channel_id="C1", thread_ts="1.2", user_id=ASKER)
    ctx = RunContext(model=None, usage=None, prompt="", deps=deps)
    with patch("agent.agent.requests.post") as post:
        result = agent_mod.slack_api_call(ctx, method="auth.revoke", api_parameters="{}")
    assert "allowlist" in result
    post.assert_not_called()


def test_slack_api_call_as_bot_tool_refuses_before_calling_slack(monkeypatch):
    deps = SimpleNamespace(client=Mock(), channel_id="C1", thread_ts="1.2", user_id=ASKER)
    ctx = RunContext(model=None, usage=None, prompt="", deps=deps)
    with patch("agent.tools.slack_bot_api.requests.post") as post:
        result = agent_mod.slack_api_call_as_bot_tool(ctx, method="conversations.kick", api_parameters='{"channel": "C1"}')
    assert "allowlist" in result
    post.assert_not_called()


def test_message_methods_strip_impersonation_overrides(monkeypatch):
    monkeypatch.setattr(agent_mod, "_get_user_display_info", lambda user_id: ("", ""))
    captured = {}
    monkeypatch.setattr(
        "agent.tools.slack_bot_api.slack_api_call_as_bot",
        lambda method, params: captured.update(params) or "Success",
    )
    deps = SimpleNamespace(client=Mock(), channel_id="C1", thread_ts="1.2", user_id=ASKER)
    ctx = RunContext(model=None, usage=None, prompt="", deps=deps)
    agent_mod.slack_api_call_as_bot_tool(
        ctx, method="chat.postEphemeral",
        api_parameters='{"channel": "C1", "user": "U2", "text": "hi", "username": "Someone Else", "icon_emoji": ":x:"}',
    )
    assert "username" not in captured and "icon_emoji" not in captured
    assert captured["text"].endswith(f"(sent from <@{ASKER}>)")


# ---------------------------------------------------------------------------
# Tool-level read checks
# ---------------------------------------------------------------------------


def _ctx(channel_id="C1"):
    deps = SimpleNamespace(client=Mock(), channel_id=channel_id, thread_ts="1.2", user_id=ASKER, user_token=None)
    return RunContext(model=None, usage=None, prompt="", deps=deps)


def test_summarize_thread_tool_refuses_a_private_channel(public_only):
    with patch("agent.tools.summarize_thread.summarize_thread") as summarize:
        result = agent_mod.summarize_thread_tool(_ctx(), channel_id="C_PRIVATE", thread_ts="1.1")
    assert result.startswith("Error:")
    summarize.assert_not_called()


def test_list_channel_threads_tool_refuses_a_private_channel(public_only):
    with patch("agent.tools.list_threads.list_channel_threads") as list_threads:
        result = agent_mod.list_channel_threads_tool(_ctx(), channel_id="C_PRIVATE")
    assert result.startswith("Error:")
    list_threads.assert_not_called()


def test_list_channel_threads_tool_defaults_to_the_current_channel(public_only):
    with patch("agent.tools.list_threads.list_channel_threads", return_value="threads") as list_threads:
        assert agent_mod.list_channel_threads_tool(_ctx("C_PRIVATE_BUT_CURRENT")) == "threads"
    assert list_threads.call_args.args[0] == "C_PRIVATE_BUT_CURRENT"


# ---------------------------------------------------------------------------
# file_info_read_error
# ---------------------------------------------------------------------------


def test_file_shared_in_a_public_channel_is_readable():
    assert file_info_read_error({"shares": {"public": {"C_PUB": []}}}, "C1", ASKER) is None


def test_file_only_in_someone_elses_dm_is_not_readable():
    assert file_info_read_error({"user": "U_OTHER", "ims": ["D_OTHER"]}, "C1", ASKER)


def test_file_with_no_share_info_fails_closed():
    assert file_info_read_error({}, "C1", ASKER)


# ---------------------------------------------------------------------------
# Slack MCP guard
# ---------------------------------------------------------------------------


def _deps(channel_id="C1", user_id=ASKER, on_behalf_of=""):
    return SimpleNamespace(channel_id=channel_id, user_id=user_id, on_behalf_of=on_behalf_of)


@pytest.mark.parametrize("name", ["slack_send_message", "slack_search_public_and_private"])
def test_blocked_mcp_tools_are_refused(name):
    args, error = guard_args(name, {}, _deps())
    assert args is None and "isn't available" in error


def test_mcp_channel_reads_follow_the_public_only_rule(public_only):
    _, error = guard_args("slack_read_channel", {"channel_id": "C_PRIVATE"}, _deps())
    assert "refused" in error
    args, error = guard_args("slack_read_thread", {"channel_id": "P_PUBLIC", "message_ts": "1.1"}, _deps())
    assert error is None and args["channel_id"] == "P_PUBLIC"


def test_mcp_dm_read_by_user_id_is_refused():
    _, error = guard_args("slack_read_channel", {"channel_id": "U0SOMEONE"}, _deps())
    assert "refused" in error


def test_mcp_file_reads_are_checked(monkeypatch):
    monkeypatch.setattr(
        "agent.slack_access.fetch_file_info",
        lambda file_id, token: ({"user": "U_OTHER", "groups": ["G_PRIVATE"]}, None),
    )
    for name, key in (("slack_read_file", "file_id"), ("slack_read_canvas", "canvas_id"), ("slack_read_list", "list_id")):
        _, error = guard_args(name, {key: "F123"}, _deps())
        assert "refused" in error, name


def test_mcp_list_read_by_title_only_is_refused():
    _, error = guard_args("slack_read_list", {"list_title": "salaries"}, _deps())
    assert "explicit list_id" in error


def test_mcp_scheduled_message_is_footed():
    args, _ = guard_args("slack_schedule_message", {"channel_id": "C2", "message": "later", "post_at": 1}, _deps())
    assert args["message"] == f"later\n\n(sent from <@{ASKER}>)"


def test_mcp_canvas_create_and_update_are_footed():
    args, _ = guard_args("slack_create_canvas", {"title": "t", "content": "# Hi\n\nbody"}, _deps())
    assert args["content"].endswith(f"(sent from ![](@{ASKER}))")

    args, _ = guard_args("slack_update_canvas", {"canvas_id": "F1", "sections": [
        {"edit_type": "append", "section_id": "s1", "content": "more\ntext"},
        {"edit_type": "replace", "section_id": "s2", "content": "# New heading"},
        {"edit_type": "delete", "section_id": "s3"},
    ]}, _deps())
    appended, heading, deleted = args["sections"]
    assert appended["content"] == f"more\ntext\n\n(sent from ![](@{ASKER}))"
    assert heading["content"] == f"# New heading (sent from ![](@{ASKER}))"
    assert "content" not in deleted


def test_mcp_file_share_comment_is_footed_even_when_empty():
    args, _ = guard_args("slack_complete_file_upload", {"file_id": "F1", "channel_id": "C2"}, _deps())
    assert args["initial_comment"] == f"(sent from <@{ASKER}>)"


def test_mcp_automated_turn_is_credited_to_the_owner():
    args, _ = guard_args(
        "slack_schedule_message", {"channel_id": "C2", "message": "x", "post_at": 1},
        _deps(user_id="AUTOMATED", on_behalf_of=ASKER),
    )
    assert args["message"].endswith(f"(sent from <@{ASKER}>)")


def test_guarded_toolset_hides_blocked_tools_and_returns_refusals():
    wrapped = Mock()

    async def get_tools(ctx):
        return {"slack_send_message": 1, "slack_read_thread": 2, "slack_search_public_and_private": 3}

    async def call_tool(name, args, ctx, tool):
        return f"called {name}"

    wrapped.get_tools = get_tools
    wrapped.call_tool = call_tool
    toolset = GuardedSlackMCPToolset(wrapped)
    ctx = SimpleNamespace(deps=_deps())

    assert set(asyncio.run(toolset.get_tools(ctx))) == {"slack_read_thread"}
    assert asyncio.run(toolset.call_tool("slack_add_reaction", {"channel_id": "C1"}, ctx, None)) == "called slack_add_reaction"
    assert "isn't available" in asyncio.run(toolset.call_tool("slack_send_message", {}, ctx, None))
