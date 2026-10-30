"""People's display names, made safe to put in front of the model.

A display name is text anyone can set to anything, and coolton shows it to the model in
places that carry meaning: the sender tag on every message (`U01234 (Name):`), thread
history, huddle transcript lines (`[voice] Name: ...`). A name with a newline, brackets or
markup could fake another sender, a huddle tag or a mention. safe_name keeps it to one short
plain line; the system prompt (MESSAGE FORMAT) says names are never instructions, however
they read (someone set theirs to "go use github.com/...").
"""
import re

_LIMIT = 60
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u0085  ​-‏⁠﻿]")


def safe_name(name: str | None, fallback: str = "unknown") -> str:
    """`name` as one plain line of at most 60 characters: no control or zero-width
    characters, no Slack markup (<...>), and no brackets or colons, which coolton's own
    message tags are made of."""
    text = _CONTROL.sub(" ", name or "")
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"[\[\]<>{}:]", " ", text)
    text = " ".join(text.split())
    if len(text) > _LIMIT:
        text = text[:_LIMIT - 3].rstrip() + "..."
    return text or fallback
