"""Checking Block Kit before coolton posts it (block_kit_validate_tool, and code channel
Block Kit tabs in agent.tools.code_channel_tools).

Slack's blocks.validate checks message rules and points at each problem. Code channel
tabs are stricter in ways it doesn't catch, and setView then only answers
`invalid_blocks` with no detail, so the tab rules found live (2026-10-11) are checked
here by hand.
"""
from __future__ import annotations

import json

MESSAGE_BLOCK_LIMIT = 50
# Blocks a code channel tab refuses although messages take them.
TAB_REFUSED_BLOCKS = {"alert": "alert blocks are for modals", "file": "file blocks"}
# Element types a tab refuses inside an actions block (fine as a section's accessory).
TAB_REFUSED_IN_ACTIONS = {"multi_static_select", "multi_users_select", "multi_conversations_select",
                          "multi_channels_select", "multi_external_select"}
# ... as a section's accessory (fine in an actions block).
TAB_REFUSED_AS_ACCESSORY = {"datetimepicker"}
# ... as an input block's element.
TAB_REFUSED_IN_INPUT = {"rich_text_input", "file_input"}


def parse_blocks(blocks) -> tuple[list | None, str | None]:
    """(the blocks as a list, None) or (None, an error for the model)."""
    if isinstance(blocks, str):
        try:
            blocks = json.loads(blocks)
        except ValueError as e:
            return None, f"Error: blocks must be JSON ({e})."
    if isinstance(blocks, dict) and isinstance(blocks.get("blocks"), list):
        blocks = blocks["blocks"]
    if not isinstance(blocks, list) or not blocks:
        return None, "Error: blocks must be a non-empty JSON array of blocks."
    return blocks, None


def tab_problems(blocks: list) -> list[str]:
    """What a code channel tab would refuse in `blocks`, each with a JSON pointer."""
    problems = []

    def check(block: dict, pointer: str) -> None:
        kind = block.get("type")
        if kind in TAB_REFUSED_BLOCKS:
            problems.append(f"{pointer}: tabs don't take {TAB_REFUSED_BLOCKS[kind]}.")
        if kind == "actions":
            for j, element in enumerate(block.get("elements") or []):
                if element.get("type") in TAB_REFUSED_IN_ACTIONS:
                    problems.append(f"{pointer}/elements/{j}: tabs don't take a {element['type']} in an actions "
                                    "block; put it in its own section block as the accessory.")
        accessory = block.get("accessory") or {}
        if kind == "section" and accessory.get("type") in TAB_REFUSED_AS_ACCESSORY:
            problems.append(f"{pointer}/accessory: tabs don't take a {accessory['type']} as a section's accessory; "
                            "put it in an actions block.")
        element = block.get("element") or {}
        if kind == "input" and element.get("type") in TAB_REFUSED_IN_INPUT:
            problems.append(f"{pointer}/element: tabs don't take a {element['type']}.")
        for j, child in enumerate(block.get("child_blocks") or []):  # container blocks
            if isinstance(child, dict):
                check(child, f"{pointer}/child_blocks/{j}")

    for i, block in enumerate(blocks):
        if isinstance(block, dict):
            check(block, f"/{i}")
    return problems


def validate(client, blocks, surface: str = "message") -> str:
    """Slack's own check (blocks.validate) plus, for a code channel tab, the tab-only
    rules. `client` is coolton's bot client."""
    parsed, error = parse_blocks(blocks)
    if parsed is None:
        return error or "Error: no blocks."
    surface = (surface or "message").strip().lower()
    problems = []
    try:
        response = client.api_call("blocks.validate", json={"blocks": parsed}).data
    except Exception as e:
        response = getattr(getattr(e, "response", None), "data", None) or {"ok": False, "error": str(e)}
    if not response.get("ok"):
        errors = response.get("errors") or []
        problems += [f"{err.get('pointer', '?')}: {err.get('message') or err.get('code')}" for err in errors]
        if not errors:
            problems.append(f"Slack refused them ({response.get('error')}).")
    if surface == "tab":
        problems += tab_problems(parsed)
    elif len(parsed) > MESSAGE_BLOCK_LIMIT - 1:
        problems.append(f"{len(parsed)} blocks: a message takes {MESSAGE_BLOCK_LIMIT}, and the automatic "
                        "\"sent from\" footer is one of them, so use at most 49.")
    if problems:
        return "Not valid (pointers are /block/...):\n" + "\n".join(f"- {p}" for p in problems)
    return f"Valid for a {'code channel tab' if surface == 'tab' else 'message'} ({len(parsed)} blocks)."
