"""`[!FAST]`: answer quickly for one turn instead of researching carefully.

By default coolton (and her subagents) ask for high reasoning effort and follow the
system prompt's RESEARCH rules: careful answers over fast ones. A message containing
`[!FAST]` (escape it as `\\[!FAST]` to send it literally) turns that off for its turn:
the model runs at its default reasoning effort and is told the thoroughness rules
don't apply (FAST_NOTE), like coolton was before.
"""
import re

_FAST_DIRECTIVE_RE = re.compile(r"(\\)?\[!FAST\]", re.IGNORECASE)

FAST_NOTE = (
    "[Fast mode: the user sent [!FAST], so this turn values speed over thoroughness. Answer "
    "quickly with the few tool calls that are clearly needed; the RESEARCH section's rules on "
    "searching wide, reading deep and not giving up early don't apply. Still cite what you "
    "found and never make things up.]\n\n"
)


def extract_fast_directive(text: str) -> tuple[str, bool]:
    """(text without any live `[!FAST]`, whether one was found). A leading backslash
    escapes it: only the backslash is dropped."""
    found = False

    def _sub(match: re.Match) -> str:
        nonlocal found
        if match.group(1):
            return match.group(0)[1:]
        found = True
        return ""

    cleaned = _FAST_DIRECTIVE_RE.sub(_sub, text)
    return (cleaned.strip() if found else cleaned), found
