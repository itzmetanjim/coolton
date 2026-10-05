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
    "auth.revoke", "admin.users.remove", "admin.conversations.archive", "usergroups.update",
    "users.profile.set", "chat.startStream", "canvases.create", "search.messages", "apps.manifest.create",
    "api.test", "reactions.list", "reminders.list", "agents.sessions.rename", "bookmarks.remove",
    "files.upload",
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


@pytest.mark.parametrize("method", [
    "auth.teams.list", "blocks.validate", "dnd.teamInfo", "team.preferences.list", "team.profile.get",
    "users.discoverableContacts.lookup", "conversations.mark", "dnd.setSnooze", "users.setPresence",
])
def test_harmless_reads_and_coolton_account_changes_are_allowed(method):
    assert check_api_call(method, {}, "C1") is None


def test_listing_methods_need_a_readable_channel(public_only):
    """Without a channel these list files / scheduled messages across every channel coolton can see."""
    for method in ("files.list", "chat.scheduledMessages.list"):
        assert "refused" in check_api_call(method, {}, "C1")
        assert "refused" in check_api_call(method, {"channel": "C_PRIVATE"}, "C1")
        assert check_api_call(method, {"channel": "C1"}, "C1") is None
    assert "refused" in check_api_call("workflows.featured.list", {"channel_ids": ["P_PUB", "C_PRIVATE"]}, "C1")
    assert check_api_call("workflows.featured.list", {"channel_ids": "P_PUB,C1"}, "C1") is None


def test_users_conversations_lists_public_channels_only():
    assert "public channels" in check_api_call("users.conversations", {"user": "U1", "types": "private_channel"}, "C1")
    assert check_api_call("users.conversations", {"user": "U1"}, "C1") is None


@pytest.mark.parametrize("method,param", [
    ("files.info", "file"), ("canvases.sections.lookup", "canvas_id"),
    ("slackLists.items.list", "list_id"), ("slackLists.items.info", "list_id"),
    ("files.sharedPublicURL", "file"),
])
def test_file_methods_follow_the_file_read_rule(monkeypatch, method, param):
    infos = {"F_PUBLIC": {"shares": {"public": {"P_PUB": []}}}, "F_THEIR_DM": {"user": "U_OTHER", "ims": ["D9"]}}
    monkeypatch.setattr(slack_access, "fetch_file_info", lambda file_id, token: (infos[file_id], None))
    assert check_api_call(method, {param: "F_PUBLIC"}, "C1", ASKER) is None
    assert "refused" in check_api_call(method, {param: "F_THEIR_DM"}, "C1", ASKER)
    assert "needs" in check_api_call(method, {}, "C1", ASKER)


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
        result = agent_mod.slack_api_call_as_bot_tool(ctx, method="admin.users.remove", api_parameters='{"user_id": "U1"}')
    assert "allowlist" in result
    post.assert_not_called()


@pytest.mark.parametrize("method,params,result,notice", [
    ("conversations.setTopic", {"channel": "C2", "topic": "x"}, {},
     ("C2", f"The channel topic was changed by <@{ASKER}>.")),
    ("bookmarks.edit", {"channel_id": "C2", "bookmark_id": "Bk1"}, {},
     ("C2", f"A bookmark was edited by <@{ASKER}>.")),
    ("conversations.create", {"name": "new"}, {"channel": {"id": "C_NEW"}},
     ("C_NEW", f"This channel was created by <@{ASKER}>.")),
    ("conversations.invite", {"channel": "C2", "users": "U1, U2"}, {},
     ("C2", f"<@U1> and <@U2> were invited here by <@{ASKER}>.")),
    ("conversations.kick", {"channel": "C2", "user": "U3"}, {},
     ("C2", f"<@U3> was removed from this channel by <@{ASKER}>.")),
    ("conversations.archive", {"channel": "C2"}, {},
     ("C2", f"This channel was archived by <@{ASKER}>.")),
])
def test_channel_changes_name_who_asked(method, params, result, notice):
    from agent.change_notices import notice_for
    assert notice_for(method, params, result, ASKER) == notice


def test_channel_change_posts_its_notice_only_after_it_succeeds(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    deps = SimpleNamespace(client=Mock(), channel_id="C1", thread_ts="1.2", user_id=ASKER)
    ctx = RunContext(model=None, usage=None, prompt="", deps=deps)
    calls = []

    def fake_post(url, data, headers, timeout):
        calls.append((url.rsplit("/", 1)[1], data))
        return Mock(json=lambda: {"ok": url.endswith("chat.postMessage") or data.get("topic") == "ok"})

    args = '{"channel": "C1", "topic": "%s"}'
    with patch("agent.agent.requests.post", fake_post), patch("agent.change_notices.requests.post", fake_post):
        agent_mod.slack_api_call(ctx, method="conversations.setTopic", api_parameters=args % "refused")
        assert [m for m, _ in calls] == ["conversations.setTopic"]
        agent_mod.slack_api_call(ctx, method="conversations.setTopic", api_parameters=args % "ok")
    assert calls[-1] == ("chat.postMessage", {"channel": "C1", "text": f"The channel topic was changed by <@{ASKER}>."})


@pytest.mark.parametrize("method,params", [
    ("conversations.invite", {"channel": "G_SECRET", "users": ASKER}),
    ("conversations.kick", {"channel": "G_SECRET", "user": "U2"}),
    ("conversations.archive", {"channel": "G_SECRET"}),
    ("conversations.rename", {"channel": "G_SECRET", "name": "x"}),
    ("conversations.setTopic", {"channel": "G_SECRET", "topic": "x"}),
    ("conversations.setPurpose", {"channel": "G_SECRET", "purpose": "x"}),
    ("bookmarks.add", {"channel_id": "G_SECRET", "title": "x", "type": "link", "link": "https://x"}),
    ("bookmarks.edit", {"channel_id": "G_SECRET", "bookmark_id": "Bk1"}),
    ("conversations.invite", {"users": ASKER}),
])
def test_a_private_channel_can_only_be_changed_from_inside_it(public_only, method, params):
    """cooltonUser is in private channels the asker isn't: inviting themselves in would
    hand them its whole history, and the rest would change a channel they can't see."""
    assert "must be a public channel or the one this conversation is in" in check_api_call(method, params, "C1", ASKER)
    in_it = {**params, ("channel_id" if "channel_id" in params else "channel"): "G_SECRET"}
    assert check_api_call(method, in_it, "G_SECRET", ASKER) is None
    public = {**params, ("channel_id" if "channel_id" in params else "channel"): "P_OPEN"}
    assert check_api_call(method, public, "C1", ASKER) is None


def _notice_calls(monkeypatch, *, archive_ok=True, notice_ok=True):
    """slack_api_call with Slack faked: returns (the tool's result, every Slack method called in order)."""
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    calls = []

    def fake_post(url, data, headers, timeout):
        method = url.rsplit("/", 1)[1]
        calls.append(method)
        ok = {"conversations.archive": archive_ok, "chat.postMessage": notice_ok}.get(method, True)
        return Mock(json=lambda: {"ok": ok, "ts": "9.9"} if ok else {"ok": False, "error": "nope"})

    deps = SimpleNamespace(client=Mock(), channel_id="C1", thread_ts="1.2", user_id=ASKER)
    ctx = RunContext(model=None, usage=None, prompt="", deps=deps)
    with patch("agent.agent.requests.post", fake_post), patch("agent.change_notices.requests.post", fake_post):
        result = agent_mod.slack_api_call(ctx, method="conversations.archive", api_parameters='{"channel": "C1"}')
    return result, calls


def test_archiving_posts_its_notice_first(monkeypatch):
    """Nothing can be posted in an archived channel, so the notice has to go out before."""
    result, calls = _notice_calls(monkeypatch)
    assert result.startswith("Success") and calls == ["chat.postMessage", "conversations.archive"]


def test_a_failed_archive_deletes_its_notice(monkeypatch):
    result, calls = _notice_calls(monkeypatch, archive_ok=False)
    assert result.startswith("Slack API error")
    assert calls == ["chat.postMessage", "conversations.archive", "chat.delete"]


def test_no_archive_without_its_notice(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")  # both tokens fail to post
    result, calls = _notice_calls(monkeypatch, notice_ok=False)
    assert result.startswith("Error: couldn't post the notice")
    assert "conversations.archive" not in calls


def test_a_file_share_is_footed():
    from agent.attribution import attribute_api_params

    shared = attribute_api_params("files.completeUploadExternal", {"files": "[]", "channel_id": "C2", "initial_comment": "here"}, ASKER)
    assert shared["initial_comment"] == f"here\n\n(sent from <@{ASKER}>)"
    bare = attribute_api_params("files.completeUploadExternal", {"files": "[]", "channels": "C2"}, ASKER)
    assert bare["initial_comment"] == f"(sent from <@{ASKER}>)"
    # not shared anywhere: nothing visible to foot
    assert attribute_api_params("files.completeUploadExternal", {"files": "[]"}, ASKER) == {"files": "[]"}


def test_a_file_share_cant_swap_its_comment_for_blocks():
    assert "initial_comment" in check_api_call(
        "files.completeUploadExternal", {"files": "[]", "channel_id": "C2", "blocks": "[]"}, "C1", ASKER)


def test_message_methods_strip_impersonation_overrides(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "agent.tools.slack_bot_api.slack_api_call_as_bot",
        lambda method, params, on_success=None: captured.update(params) or "Success",
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


@pytest.mark.parametrize("name", ["slack_send_message", "slack_search_public_and_private", "slack_search_public"])
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


def test_mcp_canvas_create_and_update_are_not_footed():
    """A canvas is one document edited repeatedly — a footer per edit piled
    up inside it, headings included."""
    create = {"title": "t", "content": "# Hi\n\nbody"}
    args, _ = guard_args("slack_create_canvas", create, _deps())
    assert args == create

    update = {"canvas_id": "F1", "sections": [{"edit_type": "replace", "section_id": "s2", "content": "# New heading"}]}
    args, _ = guard_args("slack_update_canvas", update, _deps())
    assert args == update


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
