"""What a Block Kit surface's inputs are set to when someone presses a button in it.

Selects, checkboxes, radio buttons, pickers and text inputs don't ask coolton anything on
their own (that would start a turn per tick). A button does, and its message carries the
state of every input around it, read from the block_actions payload's `state.values`.
"""

from __future__ import annotations

from typing import Any

# Interactive elements that start a turn when used. Everything else only changes state.
PROMPTING_TYPES = {"button"}


def _option(option: dict | None) -> str:
    if not option:
        return ""
    label = ((option.get("text") or {}).get("text") or "").strip()
    value = option.get("value") or ""
    return f"{label} ({value})" if label and value and label != value else label or value


def _value(element: dict) -> str:
    kind = element.get("type", "")
    if "selected_option" in element:
        return _option(element.get("selected_option")) or "nothing selected"
    if "selected_options" in element:
        chosen = [_option(o) for o in element.get("selected_options") or []]
        return ", ".join(chosen) if chosen else "nothing selected"
    for key in ("selected_date", "selected_time", "selected_date_time", "selected_user", "selected_channel",
                "selected_conversation"):
        if key in element:
            return str(element.get(key) or "not set")
    for key in ("selected_users", "selected_channels", "selected_conversations"):
        if key in element:
            return ", ".join(element.get(key) or []) or "nothing selected"
    if "value" in element:
        return element.get("value") or "empty"
    if kind == "rich_text_input":
        return "(rich text)" if element.get("rich_text_value") else "empty"
    return "not set"


def describe_state(body: dict) -> list[str]:
    """One "name: value" line per input in the surface the click came from."""
    state = body.get("state") or (body.get("view") or {}).get("state") or {}
    lines = []
    for _block_id, elements in (state.get("values") or {}).items():
        for action_id, element in (elements or {}).items():
            if isinstance(element, dict) and element.get("type") not in PROMPTING_TYPES:
                name = action_id.removeprefix("card_input_")
                lines.append(f"{name}: {_value(element)}")
    return lines


def with_state(text: str, body: dict, where: str) -> str:
    """`text`, followed by what every input in `where` is set to, if there are any."""
    lines = describe_state(body)
    if not lines:
        return text
    return f"{text}\n{where} inputs at the time:\n" + "\n".join(f"- {line}" for line in lines)


def is_prompting(action: dict[str, Any]) -> bool:
    return action.get("type") in PROMPTING_TYPES
