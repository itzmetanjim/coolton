"""Lightweight profanity / bad-word filter for coolton output.

The word list is intentionally small and editable. Extend it at runtime via
the COOLTON_BAD_WORDS env var (comma-separated) or by editing BAD_WORDS below.
"""
import os
import re

# Baseline list of words to mask from assistant output. Keep entries lowercase.
BAD_WORDS: list[str] = [
    "shit", "fuck", "fucking", "bitch", "asshole", "cunt",
    "dick", "piss", "bastard", "slut", "whore", "damn", "crap",
]

_ENV_EXTRA = os.environ.get("COOLTON_BAD_WORDS", "").strip()
if _ENV_EXTRA:
    BAD_WORDS.extend(w.strip().lower() for w in _ENV_EXTRA.split(",") if w.strip())

_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in BAD_WORDS) + r")\b",
    re.IGNORECASE,
)

_MASK = "[redacted]"


def filter_bad_words(text: str) -> str:
    """Replace any bad words in `text` with a mask.

    Word-boundary matching leaves substrings inside benign words (e.g. "scrap")
    untouched. Returns the cleaned string unchanged when empty/None.
    """
    if not text:
        return text
    return _PATTERN.sub(_MASK, text)
