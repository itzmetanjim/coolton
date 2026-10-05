"""Split a long Markdown message into parts that each fit one Slack message.

Slack caps `markdown_text` (how coolton posts replies, so Markdown renders) at 12,000
characters, and rejects a longer one with msg_too_long. split_markdown cuts a message
into parts of at most MARKDOWN_LIMIT characters that each still render correctly:

- it breaks between paragraphs where it can, otherwise between lines;
- a code block it has to cut is closed at the end of one part and reopened (with the
  same fence and language) at the start of the next;
- a line too long for one part is cut between words, never inside **bold**,
  *italics*, ~~strikethrough~~, `inline code` or a [link](url).
"""
from __future__ import annotations

import re

MARKDOWN_LIMIT = 11_000  # Slack's markdown_text cap is 12,000; leave some room
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})\s*(\S*)")


def _balanced(text: str) -> bool:
    """No inline span (code, bold, italics, strikethrough, link) left open in `text`."""
    if text.count("`") % 2:
        return False
    rest = re.sub(r"`[^`]*`", "", text)
    if any(rest.count(marker) % 2 for marker in ("**", "__", "~~")):
        return False
    if rest.count("[") != rest.count("]") or rest.count("](") != len(re.findall(r"\]\([^)]*\)", rest)):
        return False
    singles = rest.replace("**", "").replace("__", "")
    return singles.count("*") % 2 == 0 and len(re.findall(r"(?<!\w)_|_(?!\w)", singles)) % 2 == 0


def _split_line(line: str, limit: int) -> list[str]:
    """A line longer than `limit`, cut at a space outside any inline span (or, failing
    that, any space, or hard at the limit)."""
    pieces = []
    while len(line) > limit:
        spaces = [m.start() for m in re.finditer(" ", line[:limit]) if m.start() > limit // 4]
        cut = next((i for i in reversed(spaces) if _balanced(line[:i])), spaces[-1] if spaces else limit)
        pieces.append(line[:cut])
        line = line[cut:].lstrip(" ")
    return pieces + [line]


def _fence_states(lines: list[str]) -> list[tuple[str, str] | None]:
    """For each line index (and one past the end), the code block open before it, as
    (fence, language), or None."""
    states, open_fence = [], None
    for line in lines:
        states.append(open_fence)
        match = _FENCE_RE.match(line)
        if not match:
            continue
        fence, info = match.groups()
        if open_fence is None:
            open_fence = (fence, info)
        elif fence[0] == open_fence[0][0] and len(fence) >= len(open_fence[0]) and not info:
            open_fence = None
    return states + [open_fence]


def split_markdown(text: str, limit: int = MARKDOWN_LIMIT) -> list[str]:
    """`text` as parts of at most `limit` characters (see the module docstring)."""
    if len(text) <= limit:
        return [text]

    lines: list[str] = []
    for line in text.splitlines():
        lines += _split_line(line, limit // 2) if len(line) > limit // 2 else [line]
    state = _fence_states(lines)

    def render(start: int, end: int) -> str:
        """lines[start:end] as one part, reopening/closing a code block cut at either end."""
        opened, closing = state[start], state[end]
        head = [opened[0] + opened[1]] if opened else []
        tail = [closing[0]] if closing else []
        return "\n".join(head + lines[start:end] + tail)

    parts, start = [], 0
    while start < len(lines):
        end = start + 1
        while end < len(lines) and len(render(start, end + 1)) <= limit:
            end += 1
        if end < len(lines):
            # Rather break after a blank line outside a code block, if one is in the
            # second half of this part.
            blanks = [i + 1 for i in range(start, end) if not lines[i].strip() and state[i + 1] is None]
            if blanks and blanks[-1] - start >= (end - start) // 2:
                end = blanks[-1]
        part = render(start, end).strip("\n")
        if part.strip():
            parts.append(part)
        start = end
    return parts
