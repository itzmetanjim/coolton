"""Last-line enforcement of the system prompt's WRITING STYLE for final replies.

The prompt says "no em dashes, ever", but models still slip one in now and
then (especially when the reply is a tool argument, like text_only_response's).
strip_em_dashes rewrites them the way the prompt asks for (a comma), leaving
code blocks and inline code untouched.
"""
import re

_CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)


def _fix(text: str) -> str:
    text = re.sub(r"[ \t]*—[ \t]*\n", ",\n", text)          # dash ending a line
    text = re.sub(r"(^|\n)([ \t]*)—[ \t]*", r"\1\2", text)   # dash starting a line
    text = re.sub(r"[ \t]*—[ \t]*", ", ", text)              # mid-sentence
    return re.sub(r",[ \t]*([,.;:!?)])", r"\1", text)        # "—." or "—," left a stray comma


def strip_em_dashes(text: str) -> str:
    if "—" not in text:
        return text
    parts = _CODE.split(text)
    return "".join(part if i % 2 else _fix(part) for i, part in enumerate(parts))
