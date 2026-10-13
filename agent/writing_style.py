"""Last-line enforcement of the system prompt's WRITING STYLE for final replies.

The prompt says "no em dashes, ever", but models still slip one in now and
then (especially when the reply is a tool argument, like text_only_response's).
strip_em_dashes rewrites them the way the prompt asks for (a comma), leaving
code blocks and inline code untouched.

lift_tables keeps Markdown tables renderable: Slack turns a markdown_text reply into
blocks, and a table only becomes one at the top level with a blank line around it.
Nested in a list item (seen live) it shows as raw pipes.
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


_TABLE_DELIMITER = re.compile(r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?$")
_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s")


def lift_tables(text: str) -> str:
    """Every Markdown table moved to the top level (unindented) with a blank line before
    and after it. Text indented under it, until the next blank line or list item, is
    unindented too, so it can't turn into a code block. Code blocks are left alone."""
    if "|" not in text:
        return text
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        is_table = (
            not in_fence and line.strip().startswith("|") and i + 1 < len(lines)
            and _TABLE_DELIMITER.match(lines[i + 1].strip())
        )
        if not is_table:
            out.append(line)
            i += 1
            continue
        if out and out[-1].strip():
            out.append("")
        while i < len(lines) and lines[i].strip().startswith("|"):
            out.append(lines[i].strip())
            i += 1
        if i < len(lines) and lines[i].strip():
            out.append("")
            while i < len(lines) and lines[i].strip() and not _LIST_ITEM.match(lines[i]):
                out.append(lines[i].strip())
                i += 1
    return "\n".join(out)
