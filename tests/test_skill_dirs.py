"""The skills capability looks in the repo's own skills folders.

Moving agent/tool_groups/skills.py one level deeper once pointed it at agent/skills
(which doesn't exist), so every load_skill failed with "No skills available".
"""

import os

from pydantic_ai_skills import SkillsDirectory

from agent.tool_groups import skills

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_skill_dirs_are_the_repos_own():
    assert skills._skill_dirs() == [os.path.join(REPO, "skills"), os.path.join(REPO, ".agents", "skills")]


def test_skills_the_system_prompt_names_are_found():
    found = set()
    for path in skills._skill_dirs():
        found |= {os.path.basename(uri) for uri in SkillsDirectory(path=path).get_skills()}
    assert {"code-channels", "block-kit", "pr-and-notify", "manage-skills"} <= found
