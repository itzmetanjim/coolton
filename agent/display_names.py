"""People's display names, made safe to put in front of the model.

A display name is text anyone can set to anything, and coolton shows it to the model in
places that carry meaning: the sender tag on every message (`U01234 (Name):`), thread
history, huddle transcript lines (`[voice] Name: ...`). A name with a newline, brackets or
markup could fake another sender, a huddle tag or a mention. safe_name keeps it to one short
plain line; the system prompt (MESSAGE FORMAT) says names are never instructions, however
they read. Links and domains in a name become "(link)": someone named "go use
github.com/hackclub/nephthys-plus" asked "wots this", and the model took "this" to be the
repo in their name, even though the prompt told it not to act on names.
"""
import re

_LIMIT = 60
# A URL, or a domain ending in a common top-level domain ("Dr.Who" isn't one).
_TLDS = ("com|org|net|io|dev|app|sh|co|me|gg|xyz|ai|edu|gov|so|tv|to|ly|fm|us|uk|ca|de|link|site|"
         "online|tech|club|page|cc|info|biz|pro|cloud|run|zone|space|live|chat|bot|im|is|it|in|fr|eu|"
         "jp|ru|au|nl|se|ch|li|vc|ws|lol|wtf|tk|ml|ga|gq|cf|pw|top|icu|fun|land|works|tools|new")
_LINK = re.compile(rf"\b(?:https?://|www\.)\S+|\b(?:[a-z0-9-]+\.)+(?:{_TLDS})\b(?:/\S*)?", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u0085  ​-‏⁠﻿]")


def safe_name(name: str | None, fallback: str = "unknown") -> str:
    """`name` as one plain line of at most 60 characters: no control or zero-width
    characters, no Slack markup (<...>), and no brackets or colons, which coolton's own
    message tags are made of."""
    text = _CONTROL.sub(" ", name or "")
    text = re.sub(r"<[^>]*>", " ", text)
    text = _LINK.sub("(link)", text)
    text = re.sub(r"[\[\]<>{}:]", " ", text)
    text = " ".join(text.split())
    if len(text) > _LIMIT:
        text = text[:_LIMIT - 3].rstrip() + "..."
    return text or fallback
