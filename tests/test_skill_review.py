"""Skill changes from anyone but the maintainer — and every change kevinton
makes — wait for maintainer review (agent.skill_review) instead of going
live for every user straight away."""
import importlib
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic_ai import RunContext

from agent import skill_review
from agent.admin_alerts import ADMIN_USER_ID
from listeners.actions.skill_review_actions import handle_skill_review_approve, handle_skill_review_reject

agent_mod = importlib.import_module("agent.agent")
USER = "U0SOMEONE"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(agent_mod, "_skill_dirs", lambda: [str(tmp_path / "skills"), str(tmp_path / ".agents" / "skills")])
    monkeypatch.setattr(skill_review, "PROPOSALS_FILE", str(tmp_path / "skill_proposals.json"))
    dms = []
    monkeypatch.setattr(skill_review, "notify_admin", lambda text, blocks=None: dms.append((text, blocks)))
    return SimpleNamespace(root=tmp_path, dms=dms)


def _ctx(user_id=USER, **extra):
    deps = SimpleNamespace(client=Mock(), channel_id="C1", thread_ts="1.2", user_id=user_id, **extra)
    return RunContext(model=None, usage=None, prompt="", deps=deps)


def _proposal_id(dms):
    blocks = dms[-1][1]
    actions = next(b for b in blocks if b["type"] == "actions")["elements"]
    return actions[0]["value"]


def _click(handler, proposal_id, user_id=ADMIN_USER_ID):
    client = Mock()
    body = {
        "user": {"id": user_id}, "actions": [{"value": proposal_id}],
        "channel": {"id": "D_ADMIN"}, "message": {"ts": "9.9"},
    }
    handler(Mock(), body, client, Mock())
    return client


def test_maintainer_creates_a_skill_immediately(repo):
    result = agent_mod.create_skill(_ctx(ADMIN_USER_ID), "Cool Skill", "does a thing", "Steps.")
    assert result.startswith("Created skill 'cool-skill'")
    assert (repo.root / "skills" / "cool-skill" / "SKILL.md").exists()
    assert repo.dms == []


def test_anyone_else_proposes_instead_of_creating(repo):
    result = agent_mod.create_skill(_ctx(), "Cool Skill", "does a thing", "Steps.")
    assert "Submitted for review" in result
    assert not (repo.root / "skills" / "cool-skill").exists()
    text, blocks = repo.dms[-1]
    assert f"<@{USER}>" in text
    assert "Steps." in str(blocks)  # the reviewer sees the actual SKILL.md


def test_kevinton_always_goes_through_review_even_on_the_maintainers_turn(repo):
    result = agent_mod.create_skill(
        _ctx(ADMIN_USER_ID, skill_review_required=True), "Cool Skill", "does a thing", "Steps.",
    )
    assert "Submitted for review" in result
    assert "kevinton" in repo.dms[-1][0]
    assert not (repo.root / "skills" / "cool-skill").exists()


def test_approving_a_proposal_applies_it_and_tells_the_requester(repo):
    agent_mod.create_skill(_ctx(), "Cool Skill", "does a thing", "Steps.")
    client = _click(handle_skill_review_approve, _proposal_id(repo.dms))
    assert (repo.root / "skills" / "cool-skill" / "SKILL.md").exists()
    assert "Created skill" in client.chat_update.call_args.kwargs["text"]
    assert client.chat_postMessage.call_args.kwargs["channel"] == USER


def test_rejecting_a_proposal_changes_nothing(repo):
    agent_mod.create_skill(_ctx(), "Cool Skill", "does a thing", "Steps.")
    client = _click(handle_skill_review_reject, _proposal_id(repo.dms))
    assert not (repo.root / "skills" / "cool-skill").exists()
    assert "Rejected" in client.chat_update.call_args.kwargs["text"]


def test_only_the_maintainer_can_approve(repo):
    agent_mod.create_skill(_ctx(), "Cool Skill", "does a thing", "Steps.")
    proposal_id = _proposal_id(repo.dms)
    client = _click(handle_skill_review_approve, proposal_id, user_id=USER)
    assert not (repo.root / "skills" / "cool-skill").exists()
    client.chat_update.assert_not_called()
    # still pending for the real maintainer
    _click(handle_skill_review_approve, proposal_id)
    assert (repo.root / "skills" / "cool-skill" / "SKILL.md").exists()


def test_a_proposal_cant_be_applied_twice(repo):
    agent_mod.create_skill(_ctx(), "Cool Skill", "does a thing", "Steps.")
    proposal_id = _proposal_id(repo.dms)
    _click(handle_skill_review_approve, proposal_id)
    client = _click(handle_skill_review_approve, proposal_id)
    assert "already handled" in client.chat_update.call_args.kwargs["text"]


def test_delete_and_rename_wait_for_review(repo):
    skill = repo.root / "skills" / "old-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: old-skill\ndescription: x\n---\n")

    assert "Submitted for review" in agent_mod.delete_skill(_ctx(), "old-skill")
    assert "Submitted for review" in agent_mod.rename_skill(_ctx(), "old-skill", "new-skill")
    assert skill.exists()


def test_approved_rename_is_applied(repo):
    skill = repo.root / "skills" / "old-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: old-skill\ndescription: x\n---\n")
    agent_mod.rename_skill(_ctx(), "old-skill", "new-skill")
    _click(handle_skill_review_approve, _proposal_id(repo.dms))
    assert "name: new-skill" in (repo.root / "skills" / "new-skill" / "SKILL.md").read_text()


def _staged_install(repo):
    staging = skill_review.new_staging_dir(str(repo.root))
    os.makedirs(os.path.join(staging, "cool-skill", "scripts"))
    with open(os.path.join(staging, "cool-skill", "SKILL.md"), "w") as f:
        f.write("---\nname: cool-skill\ndescription: x\n---\n")
    with open(os.path.join(staging, "cool-skill", "scripts", "run.sh"), "w") as f:
        f.write("echo hi\n")
    return staging


def test_approved_install_moves_the_staged_files_into_place(repo):
    staging = _staged_install(repo)
    spec = {"op": "install", "staged_dir": staging, "slugs": ["cool-skill"]}
    skill_review.submit(spec, _ctx().deps, "install cool-skill")
    _click(handle_skill_review_approve, _proposal_id(repo.dms))
    installed = repo.root / ".agents" / "skills" / "cool-skill"
    assert (installed / "scripts" / "run.sh").read_text() == "echo hi\n"
    assert not os.path.exists(staging)


def test_rejected_install_deletes_the_staged_files(repo):
    staging = _staged_install(repo)
    spec = {"op": "install", "staged_dir": staging, "slugs": ["cool-skill"]}
    skill_review.submit(spec, _ctx().deps, "install cool-skill")
    _click(handle_skill_review_reject, _proposal_id(repo.dms))
    assert not os.path.exists(staging)
    assert not (repo.root / ".agents" / "skills" / "cool-skill").exists()
