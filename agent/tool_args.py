"""Parsing the arguments the model passes to tools."""

import json


def parse_json_object_param(param_name: str, value: str, example: str = '{"key": "value"}') -> tuple[dict | None, str | None]:
    """Parse `value` (a JSON-encoded object) into a dict. Returns (parsed, None) on
    success, or (None, error_message) on failure.

    Confirmed live: a schema-less nested `object` parameter (no defined properties,
    just `{"type": "object"}`) gives the model nothing to anchor against, and at least
    one model/provider combination reliably sent an empty `{}` for it even when it
    could correctly state in plain English what the value should contain — renaming
    the field alone didn't fix it, but switching to a plain string the model fills
    with JSON text (an ordinary language-model-native task) did. Every tool that used
    to take a schema-less `dict` parameter now takes a JSON-encoded string instead and
    parses it here.
    """
    text = (value or "").strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return None, (
            f"Error: {param_name} must be valid JSON (a plain object, e.g. "
            f"'{example}') — got invalid JSON: {e}"
        )
    if not isinstance(parsed, dict):
        return None, (
            f"Error: {param_name} must be a JSON object (e.g. '{example}'), "
            f"not a {type(parsed).__name__}."
        )
    return parsed, None
