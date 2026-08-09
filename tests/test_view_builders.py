from listeners.views.app_home_builder import build_app_home_view
from listeners.views.feedback_builder import build_feedback_blocks


def test_build_feedback_blocks():
    blocks = build_feedback_blocks()

    assert len(blocks) > 0
    # The block should contain a feedback action
    block_dict = blocks[0].to_dict()
    action_ids = [e["action_id"] for e in block_dict["elements"]]
    assert "feedback" in action_ids


def _section_texts(view):
    return [b["text"]["text"] for b in view["blocks"] if b["type"] == "section"]


def _button_ids(view):
    return [
        el["action_id"]
        for b in view["blocks"]
        if b["type"] == "actions"
        for el in b["elements"]
    ]


def test_build_app_home_view_default():
    """Default args (Socket Mode) — shows disconnected status with learn-more link."""
    view = build_app_home_view()

    assert view["type"] == "home"

    # Should have a header and a section
    block_types = [b["type"] for b in view["blocks"]]
    assert "header" in block_types
    assert "section" in block_types

    # Shows MCP status as disconnected
    mcp_section = next(t for t in _section_texts(view) if "Slack MCP Server" in t)
    assert "disconnected" in mcp_section


def test_build_app_home_view_connect():
    """install_url provided — shows disconnected status with install link."""
    view = build_app_home_view(install_url="https://example.com/slack/install")

    mcp_section = next(t for t in _section_texts(view) if "Slack MCP Server" in t)
    assert "disconnected" in mcp_section
    assert "https://example.com/slack/install" in mcp_section


def test_build_app_home_view_connected():
    """is_connected=True — shows connected status."""
    view = build_app_home_view(is_connected=True)

    mcp_section = next(t for t in _section_texts(view) if "Slack MCP Server" in t)
    assert "connected" in mcp_section


def test_byok_status_not_configured():
    view = build_app_home_view()
    text = " ".join(_section_texts(view))
    assert "not configured" in text


def test_byok_lists_endpoints():
    endpoints = [
        {"id": "ep_1", "name": "Mine", "model": "gpt-4o", "base_url": "https://api.openai.com/v1"},
        {"id": "ep_2", "name": "Ollama", "model": "llama3", "base_url": "http://localhost:11434/v1"},
    ]
    view = build_app_home_view(endpoints=endpoints, text_endpoint_id="ep_1")
    text = " ".join(_section_texts(view))
    assert "2 endpoints configured" in text
    assert "Mine" in text
    assert "gpt-4o" in text
    # text default tag shown only on the selected endpoint
    assert "_[text]_" in text
    assert "_[text, image]_" not in text


def test_byok_default_select_initial_option():
    endpoints = [{"id": "ep_1", "name": "Mine", "model": "gpt-4o", "base_url": "https://x"}]
    view = build_app_home_view(endpoints=endpoints, text_endpoint_id="ep_1", image_endpoint_id="ep_1")

    selects = [
        b["accessory"]
        for b in view["blocks"]
        if b["type"] == "section" and b.get("accessory", {}).get("type") == "static_select"
    ]
    text_select = next(s for s in selects if s["action_id"] == "byok_select_text")
    assert text_select["initial_option"]["value"] == "ep_1"

    image_select = next(s for s in selects if s["action_id"] == "byok_select_image")
    assert image_select["initial_option"]["value"] == "ep_1"


def test_custom_instructions_section_and_button():
    view = build_app_home_view()
    text = " ".join(_section_texts(view))
    assert "No custom instructions yet" in text
    assert "instructions_open" in _button_ids(view)


def test_custom_instructions_active():
    view = build_app_home_view(has_instructions=True)
    text = " ".join(_section_texts(view))
    assert "custom instructions are active" in text


def test_reminders_section_no_pending():
    view = build_app_home_view()
    text = " ".join(_section_texts(view))
    assert "No pending reminders" in text


def test_reminders_section_lists_pending():
    reminders = [
        {"id": "r1", "text": "buy milk and eggs and bread", "due_at": 1700000000, "sent": False},
        {"id": "r2", "text": "already sent", "due_at": 1700000000, "sent": True},
    ]
    view = build_app_home_view(reminders=reminders)
    text = " ".join(_section_texts(view))
    assert "1 pending reminder" in text
    assert "`#r1`" in text
    assert "buy milk and eggs and bread" in text
    assert "already sent" not in text


def test_reminders_section_limits_preview():
    reminders = [
        {"id": f"r{i}", "text": f"task {i}", "due_at": 1700000000, "sent": False}
        for i in range(7)
    ]
    view = build_app_home_view(reminders=reminders)
    text = " ".join(_section_texts(view))
    assert "7 pending reminders" in text
    assert "...and 2 more" in text


def test_fallback_cache_actions_present():
    view = build_app_home_view()
    buttons = _button_ids(view)
    assert "fallback_cache_clear" in buttons
    assert "test_providers" in buttons
