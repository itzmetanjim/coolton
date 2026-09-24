"""The "(sent from <@user>)" footer is enforced in code, not the prompt, so the
tests here drive the real tools the model calls and check what actually reaches
Slack — a prompt-injected "leave the footer off" can't change any of this."""
import importlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic_ai import RunContext

from agent.attribution import attribute_api_params, attribute_text, attribution_user_id

ASKER = "U0ASKER1"
FOOTER = f"(sent from <@{ASKER}>)"
agent_mod = importlib.import_module("agent.agent")


def _ctx(client=None):
    deps = SimpleNamespace(client=client or Mock(), channel_id="C1", thread_ts="1.2", user_id=ASKER)
    return RunContext(model=None, usage=None, prompt="", deps=deps)


def test_attribute_text_appends_footer_after_a_blank_line():
    assert attribute_text("all good!", ASKER) == f"all good!\n\n{FOOTER}"


def test_attribute_text_is_idempotent():
    once = attribute_text("hi", ASKER)
    assert attribute_text(once, ASKER) == once


def test_attribute_text_never_leaves_a_message_unfooted():
    """A turn with no real user behind it still says so, instead of posting
    with no footer at all."""
    assert attribute_text("job finished", "AUTOMATED") == "job finished\n\n(sent automatically by coolton)"


def test_automated_turns_are_credited_to_the_job_owner():
    deps = SimpleNamespace(user_id="AUTOMATED", on_behalf_of=ASKER)
    assert attribution_user_id(deps) == ASKER


def test_a_real_sender_wins_over_on_behalf_of():
    deps = SimpleNamespace(user_id="U0OTHER", on_behalf_of=ASKER)
    assert attribution_user_id(deps) == "U0OTHER"


def test_attachments_only_message_gets_the_footer_as_text():
    params = attribute_api_params("chat.postMessage", {"channel": "C1", "attachments": "[]"}, ASKER)
    assert params["text"] == FOOTER


def test_chat_post_message_tool_foots_the_message(monkeypatch):
    monkeypatch.setattr(agent_mod, "_inject_poster", lambda params, user_id: params)
    client = Mock()
    client.chat_postMessage.return_value = {"ok": True}
    agent_mod.chat_postMessage(_ctx(client), channel="C0OTHER", text="all good!")
    assert client.chat_postMessage.call_args.kwargs["markdown_text"] == f"all good!\n\n{FOOTER}"


def test_post_message_tool_foots_the_message(monkeypatch):
    monkeypatch.setattr(agent_mod, "_get_user_display_info", lambda user_id: ("", ""))
    with patch("agent.tools.slack_info.post_message_to_target", return_value="ok") as post:
        agent_mod.post_message_tool(_ctx(), channel_id="C1", text="hello")
    assert post.call_args.kwargs["text"] == f"hello\n\n{FOOTER}"


def test_slack_api_call_foots_post_message_as_cooltonuser(monkeypatch):
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    monkeypatch.setattr(agent_mod, "_inject_poster", lambda params, user_id: params)
    with patch("agent.agent.requests.post") as post:
        post.return_value.json.return_value = {"ok": True}
        agent_mod.slack_api_call(
            _ctx(), method="chat.postMessage", api_parameters='{"channel": "C0OTHER", "text": "all good!"}'
        )
    assert post.call_args.kwargs["data"]["text"] == f"all good!\n\n{FOOTER}"


def test_slack_api_call_as_bot_foots_an_edit_including_its_blocks(monkeypatch):
    """chat.update with blocks: Slack renders the blocks, not `text`, so the
    footer has to land in the blocks too or the edit shows unattributed."""
    captured = {}
    monkeypatch.setattr(
        "agent.tools.slack_bot_api.slack_api_call_as_bot",
        lambda method, params: captured.update(method=method, params=params) or "Success",
    )
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "edited"}}]
    agent_mod.slack_api_call_as_bot_tool(
        _ctx(), method="chat.update",
        api_parameters=json.dumps({"channel": "C1", "ts": "1.3", "text": "edited", "blocks": json.dumps(blocks)}),
    )
    params = captured["params"]
    assert params["text"].endswith(FOOTER)
    sent_blocks = json.loads(params["blocks"])
    assert sent_blocks[0] == blocks[0]
    assert sent_blocks[-1]["elements"][0]["text"] == FOOTER


def test_non_message_methods_are_left_alone():
    params = {"channel": "C1", "text": "not a message"}
    assert attribute_api_params("conversations.setTopic", params, ASKER) == params


def test_method_with_smuggled_query_string_is_refused(monkeypatch):
    """`chat.postMessage?text=...` would reach slack.com carrying message
    params the footer logic never looked at."""
    monkeypatch.setenv("SLACK_USER_TOKEN", "xoxp-test")
    with patch("agent.agent.requests.post") as post:
        result = agent_mod.slack_api_call(
            _ctx(), method="chat.postMessage?text=unattributed", api_parameters='{"channel": "C1"}'
        )
    assert result.startswith("Error: invalid Slack API method")
    post.assert_not_called()


def test_slack_mcp_toolset_is_guarded():
    from agent.platforms.slack import SlackPlatform
    from agent.slack_mcp_guard import GuardedSlackMCPToolset

    with patch("agent.platforms.slack.MCPToolset", return_value=Mock()), \
         patch("agent.mcp_server_store.get_user_servers", return_value=[]):
        toolset = SlackPlatform().toolsets(SimpleNamespace(user_id=ASKER, user_token="xoxp-test"))[0]
    assert isinstance(toolset, GuardedSlackMCPToolset)
