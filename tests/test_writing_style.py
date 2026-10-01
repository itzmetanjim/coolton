"""coolton's replies never contain em dashes (WRITING STYLE in the system prompt)."""
import importlib
from pathlib import Path

from agent.writing_style import strip_em_dashes


def test_em_dashes_become_commas_outside_code():
    reply = "ohhh got it—housewarming chaos, not sadness.\nfixed it — see:\n```py\nx = 'a—b'\n```\nand `c—d` stays"
    assert strip_em_dashes(reply) == (
        "ohhh got it, housewarming chaos, not sadness.\nfixed it, see:\n```py\nx = 'a—b'\n```\nand `c—d` stays"
    )
    assert strip_em_dashes("done—.") == "done."


def test_the_prompt_and_tool_descriptions_have_no_em_dashes():
    """The model copies the style it's shown: a prompt full of em dashes makes
    "no em dashes, ever" a rule it keeps breaking."""
    prompt = Path("agent/platforms/system_prompt.md").read_text()
    assert "—" not in prompt
    tools = importlib.import_module("agent.agent").agent._function_toolset.tools
    assert [name for name, tool in tools.items() if "—" in (tool.description or "")] == []
