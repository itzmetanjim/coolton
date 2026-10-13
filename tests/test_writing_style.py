"""coolton's replies never contain em dashes (WRITING STYLE in the system prompt)."""
import importlib
from pathlib import Path

from agent.writing_style import lift_tables, strip_em_dashes


def test_em_dashes_become_commas_outside_code():
    reply = "ohhh got it—housewarming chaos, not sadness.\nfixed it — see:\n```py\nx = 'a—b'\n```\nand `c—d` stays"
    assert strip_em_dashes(reply) == (
        "ohhh got it, housewarming chaos, not sadness.\nfixed it, see:\n```py\nx = 'a—b'\n```\nand `c—d` stays"
    )
    assert strip_em_dashes("done—.") == "done."


def test_a_table_in_a_list_item_moves_to_the_top_level():
    """Slack only renders a markdown_text table at the top level (seen live as raw pipes)."""
    reply = (
        "1. first\n"
        "2. the valid ones are:\n"
        "    | Portfolio | Effort |\n"
        "    |---|---:|\n"
        "    | A1, B1 | 10 |\n"
        "    the best is 34.\n"
        "3. third\n"
        "```\n| a |\n|---|\n```"
    )
    assert lift_tables(reply) == (
        "1. first\n"
        "2. the valid ones are:\n"
        "\n"
        "| Portfolio | Effort |\n"
        "|---|---:|\n"
        "| A1, B1 | 10 |\n"
        "\n"
        "the best is 34.\n"
        "3. third\n"
        "```\n| a |\n|---|\n```"
    )


def test_the_prompt_and_tool_descriptions_have_no_em_dashes():
    """The model copies the style it's shown: a prompt full of em dashes makes
    "no em dashes, ever" a rule it keeps breaking."""
    prompt = Path("agent/platforms/system_prompt.md").read_text()
    assert "—" not in prompt
    tools = importlib.import_module("agent.agent").agent._function_toolset.tools
    assert [name for name, tool in tools.items() if "—" in (tool.description or "")] == []
