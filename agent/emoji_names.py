"""Other names to try when Slack rejects a reaction's emoji name (invalid_name).

Models often use an emoji's official Unicode name where Slack knows it by a short alias
(light_bulb for bulb), drop part of it (zipper_mouth for zipper_mouth_face), or add
colons. These are the same emoji under the names Slack might know it by, from the
`emoji` package's data (official names plus their GitHub-style aliases, which are what
Slack's built-in names mostly are). No guessing by spelling: "clock" isn't "lock".
"""
from __future__ import annotations

import functools

# Reacted with instead when no name for the emoji works: coolton's "seen it".
FALLBACK = "eyes"


def normalize(name: str) -> str:
    return name.strip().strip(":").strip().lower().replace(" ", "_").replace("-", "_").replace("’", "")


@functools.cache
def _names_by_name() -> dict[str, tuple[str, ...]]:
    import emoji

    index: dict[str, tuple[str, ...]] = {}
    for data in emoji.EMOJI_DATA.values():
        names = tuple(dict.fromkeys(normalize(n) for n in [*data.get("alias", []), data["en"]]))
        for name in names:
            index.setdefault(name, names)
    return index


def alternatives(name: str, limit: int = 3) -> list[str]:
    """Names to try after `name` was rejected, best first (not including `name`)."""
    wanted = normalize(name)
    found = [wanted]
    index = _names_by_name()
    for key in (wanted, f"{wanted}_face"):
        found.extend(index.get(key, ()))
    return [n for n in dict.fromkeys(found) if n and n != name][:limit]
