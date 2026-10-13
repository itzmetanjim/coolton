"""coolton_convs.py, the standalone conversation viewer, reads what coolton's stores write."""
from datetime import datetime, timezone

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

import coolton_convs
from thread_context.store import ConversationStore
from thread_context.training_log import ConversationTraceStore
from web import conversation_log

PROMPT = (
    "## CURRENT CONTEXT\n- This conversation is a private channel (#secret-plans)\n"
    "- Your own bot user id (this is YOU, not a third party): `U0BOT00001`\n\n"
    "[Turn context — message timestamp: `1.1`, model: openai/gpt-6-luna]\n\n"
    "U0LILY0001 (Lily ^-^ (KitKat)):\n<@U0BOT00001> what's the plan?"
)


def _turn() -> list:
    now = datetime.now(timezone.utc)
    return [
        ModelRequest(parts=[SystemPromptPart(content="you are coolton"), UserPromptPart(content=PROMPT, timestamp=now)]),
        ModelResponse(parts=[ToolCallPart(tool_name="search_web", args={"query": "plans"}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="search_web", content="Error: offline", tool_call_id="c1")]),
        ModelResponse(parts=[ToolCallPart(tool_name="text_only_response", tool_call_id="c2",
                                          args={"emoji_name": "eyes", "response": "no plan yet"})]),
    ]


def test_reads_slack_traces_and_stored_history(tmp_path):
    slack = [{"user": "U0LILY0001", "ts": "1.1", "text": "<@U0BOT00001> what's the plan?"}]
    ConversationTraceStore(str(tmp_path / "conversation_logs")).write("C0SECRET01", "1.1", _turn(), slack)
    ConversationStore(directory=str(tmp_path / "conversations"), legacy_file=None).set_history("C0SECRET01", "1.1", _turn())

    scanned = coolton_convs.scan(str(tmp_path))
    assert sorted(s["format"] for s in scanned["summaries"]) == ["history", "trace"]
    for summary in scanned["summaries"]:
        conv = coolton_convs.load_conversation(summary["path"], summary["subkey"])
        assert (conv["title"], conv["where"], conv["people"]) == (
            "what's the plan?", "#secret-plans (private)", ["Lily ^-^ (KitKat)"])
        system, user, tool, output = conv["entries"]
        assert system["kind"] == "system"
        assert user["text"] == "<@U0BOT00001> what's the plan?" and "CURRENT CONTEXT" in user["context"]
        assert (tool["title"], tool["args"], tool["status"]) == ("search_web", {"query": "plans"}, "error")
        assert (output["kind"], output["text"], output["emoji"]) == ("output", "no plan yet", "eyes")
        if conv["format"] == "trace":
            assert conv["slack"][0]["who"] == "Lily ^-^ (KitKat)"


def test_reads_web_conversations(tmp_path, monkeypatch):
    monkeypatch.setattr(conversation_log, "STORE_DIR", str(tmp_path))
    conversation_id = conversation_log.create_conversation("U0LILY0001", title="plans")
    for event in (
        {"type": "user_message", "text": "what's the plan?", "user_id": "U0LILY0001", "attachments": []},
        {"type": "step", "kind": "tool", "step_id": "s1", "tool_name": "search_web", "status": "in_progress",
         "args": {"query": "plans"}},
        {"type": "step", "kind": "tool", "step_id": "s1", "tool_name": "search_web", "status": "complete",
         "result": "nothing yet"},
        {"type": "agent_message", "variant": "final", "text": "no plan yet"},
    ):
        conversation_log.append_event(conversation_id, event)

    (summary,) = coolton_convs.scan(str(tmp_path))["summaries"]
    conv = coolton_convs.load_conversation(summary["path"], summary["subkey"])
    assert (conv["title"], conv["format"], conv["turns"], conv["tools"]) == ("plans", "web", 1, 1)
    assert [(e["kind"], e.get("text") or e.get("result")) for e in conv["entries"]] == [
        ("user", "what's the plan?"), ("tool", "nothing yet"), ("output", "no plan yet")]
