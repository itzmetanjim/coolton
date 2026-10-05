"""agent.message_split: long Markdown replies split into parts that each fit one Slack
message and still render."""
import re

from agent.message_split import _balanced, split_markdown


def test_a_short_message_is_one_part():
    assert split_markdown("hi **there**", limit=100) == ["hi **there**"]


def test_a_code_block_cut_in_two_is_closed_and_reopened_with_its_language():
    code = "```python\n" + "\n".join(f"print({i})" for i in range(300)) + "\n```"
    parts = split_markdown(f"intro\n\n{code}\n\noutro", limit=1000)

    assert len(parts) > 2 and all(len(p) <= 1000 for p in parts)
    assert all(p.count("```") % 2 == 0 for p in parts)  # every part's code block is closed
    assert all(p.startswith("```python") for p in parts[1:-1])
    lines = [line for p in parts for line in p.splitlines() if line.startswith("print(")]
    assert lines == [f"print({i})" for i in range(300)]  # nothing lost or repeated


def test_a_long_line_is_never_cut_inside_formatting():
    line = "see **bold words here** and `some code` and [a link](https://example.com/x) " * 100
    parts = split_markdown(line, limit=700)

    assert len(parts) > 1 and all(len(p) <= 700 for p in parts)
    assert all(_balanced(p) for p in parts)
    assert " ".join(parts).split() == line.split()


def test_it_breaks_between_paragraphs_when_it_can():
    paragraphs = [f"paragraph {i}: " + "words " * 30 for i in range(20)]
    parts = split_markdown("\n\n".join(paragraphs), limit=1000)

    for part in parts:
        assert re.match(r"paragraph \d+: ", part) and part.rstrip().endswith("words")
