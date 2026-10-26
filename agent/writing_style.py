"""Last-line enforcement of the system prompt's WRITING STYLE for final replies.

The prompt says "no em dashes, ever", but models still slip one in now and
then (especially when the reply is a tool argument, like text_only_response's).
strip_em_dashes rewrites them the way the prompt asks for (a comma), leaving
code blocks and inline code untouched.

lowercase_sentence_starts keeps the prompt's "reply in lowercase" from drifting: models
lowercase a reply's first sentence and then go back to capitalizing the rest.

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


# Words a capital at the start of a sentence is only there by habit for. Names, acronyms and
# mixed case (OmniRush, LPM, GitHub) aren't in it, and keep their case.
_COMMON = set("""
a about above after again against all also although always an and another any anything are
around as at be because been before being both but by can could did do does doing done each
either even every everything few first for from further had has have having he her here hers
him his how however i if in instead into is it its itself just last later least less let like
maybe more most much must my neither never next no none nor not nothing now of off often on once
one only or other otherwise our out over own perhaps please plus quite rather really same see
she should since so some something sometimes still such than that the their them then there
these they this those though through thus to too under unless until up us very was we were
what whatever when where whether which while who whom whose why will with within without would
yes yet you your yours
yeah nope ok okay sure fine good bad great right wrong true false hey hi note also
add check run use try make get go set keep put take give find look read open close start stop
it's that's there's here's what's i'm i've i'd i'll you're you've they're we're don't doesn't
didn't isn't aren't wasn't weren't can't couldn't won't wouldn't shouldn't haven't hasn't
""".split())
_SENTENCE_START = re.compile(
    r"(?:(?<=^)|(?<=\n)|(?<=[.!?] )|(?<=[.!?]\) )|(?<=: ))"     # start of text/line/sentence
    r"((?:[ \t]*(?:[-*+]|\d+[.)])[ \t]+)?)"                      # a list marker
    r"([A-Z][a-z]*(?:'[a-z]+)?)\b"                                # a Capitalized word
)
_KEEP = re.compile(r"(```.*?```|`[^`\n]*`|<[^>\n]*>|\[[^\]\n]*\]\([^)\n]*\)|^>.*$)", re.S | re.M)


def lowercase_sentence_starts(text: str) -> str:
    """Lowercase the habitual capital starting a sentence or list item, in a reply the
    model began in lowercase (coolton's style). One that begins capitalized is left alone:
    that's a deliberate draft (an email, an announcement), not drift. Only ordinary words
    change; code, links, mentions and quotes are left as they are."""
    first = re.search(r"[A-Za-z]", _KEEP.sub("", text))
    if not first or first.group().isupper():
        return text
    seen_lower = set(re.findall(r"\b[a-z][a-z']*\b", _KEEP.sub(" ", text)))

    def fix(m):
        word = m.group(2)
        lower = word.lower()
        if word == "I":
            return m.group(1) + "i"
        if lower in _COMMON or lower in seen_lower:
            return m.group(1) + lower
        return m.group(0)

    parts = _KEEP.split(text)
    return "".join(part if i % 2 else _SENTENCE_START.sub(fix, part) for i, part in enumerate(parts))
