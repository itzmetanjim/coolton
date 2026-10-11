"""agent.tools.block_kit: checking Block Kit before coolton posts it or puts it in a code
channel tab, which is stricter than Slack's blocks.validate (rules found live)."""
from unittest.mock import Mock

from agent.tools import block_kit

PT = {"type": "plain_text", "text": "x"}
MULTI = {"type": "multi_static_select", "action_id": "m", "placeholder": PT, "options": [{"text": PT, "value": "a"}]}


def test_tab_only_rules_are_found_with_pointers_even_inside_a_container():
    blocks = [
        {"type": "actions", "elements": [{"type": "button", "text": PT, "action_id": "b"}, MULTI]},
        {"type": "section", "text": PT, "accessory": MULTI},  # fine as an accessory
        {"type": "section", "text": PT, "accessory": {"type": "datetimepicker", "action_id": "d"}},
        {"type": "input", "label": PT, "element": {"type": "rich_text_input", "action_id": "r"}},
        {"type": "container", "title": PT, "child_blocks": [{"type": "alert", "text": "careful"}]},
    ]

    problems = block_kit.tab_problems(blocks)

    assert [p.split(":")[0] for p in problems] == ["/0/elements/1", "/2/accessory", "/3/element", "/4/child_blocks/0"]


def test_validate_reports_slacks_pointers_and_the_tab_rules_together():
    client = Mock()
    client.api_call.return_value = Mock(data={"ok": False, "error": "invalid_blocks", "errors": [
        {"pointer": "/1/rows/1/1", "message": "failed to match any allowed schemas"}]})
    blocks = [{"type": "actions", "elements": [MULTI]}, {"type": "table", "rows": []}]

    report = block_kit.validate(client, blocks, surface="tab")

    assert "/1/rows/1/1: failed to match any allowed schemas" in report and "/0/elements/0" in report
    assert block_kit.validate(client, "[]").startswith("Error")


def test_a_message_has_room_for_49_blocks_of_its_own():
    client = Mock()
    client.api_call.return_value = Mock(data={"ok": True})
    assert block_kit.validate(client, [{"type": "divider"}] * 49).startswith("Valid")
    assert "at most 49" in block_kit.validate(client, [{"type": "divider"}] * 50)
