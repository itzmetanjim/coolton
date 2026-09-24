"""Maintainer review for skill changes.

Skills are global: every skill in skills/ and .agents/skills/ is offered to
every user's turns, so one bad skill is a standing prompt injection for
everyone. A skill change (create, install, rename, delete) therefore only
takes effect immediately when the maintainer (agent.admin_alerts.ADMIN_USER_ID)
asks for it directly. Anyone else's request — and every change kevinton makes
on its own, since it works from untrusted transcripts — becomes a proposal:
stored here, DM'd to the maintainer with Approve/Reject buttons
(listeners/actions/skill_review_actions.py), and only applied on approval
(agent.agent.apply_skill_change).

A proposal's `spec` is exactly what apply_skill_change consumes. An install's
fetched files are staged under <repo>/STAGING_DIR until approved or rejected.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid

from agent.admin_alerts import ADMIN_USER_ID, notify_admin

PROPOSALS_FILE = "skill_proposals.json"
STAGING_DIR = "skill_proposals"
_PREVIEW_CHARS = 2500

_lock = threading.Lock()


def needs_review(deps) -> bool:
    return bool(getattr(deps, "skill_review_required", False)) or getattr(deps, "user_id", "") != ADMIN_USER_ID


def new_staging_dir(repo_root: str) -> str:
    path = os.path.join(os.path.abspath(repo_root), STAGING_DIR, uuid.uuid4().hex[:12])
    os.makedirs(path, exist_ok=True)
    return path


def _load() -> dict:
    try:
        with open(PROPOSALS_FILE) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    temp = f"{PROPOSALS_FILE}.tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, PROPOSALS_FILE)


def _review_blocks(proposal_id: str, summary: str, preview: str) -> list[dict]:
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": summary}}]
    if preview:
        clipped = preview if len(preview) <= _PREVIEW_CHARS else preview[:_PREVIEW_CHARS] + "\n…(truncated)"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{clipped.replace('```', 'ʼʼʼ')}```"}})
    blocks.append({"type": "actions", "elements": [
        {"type": "button", "action_id": "skill_review_approve", "value": proposal_id, "style": "primary",
         "text": {"type": "plain_text", "text": "Approve"}},
        {"type": "button", "action_id": "skill_review_reject", "value": proposal_id, "style": "danger",
         "text": {"type": "plain_text", "text": "Reject"}},
    ]})
    return blocks


def submit(spec: dict, deps, description: str, preview: str = "") -> str:
    """Store a proposed skill change, DM it to the maintainer for review, and
    return the message for the model to relay."""
    proposal_id = uuid.uuid4().hex[:10]
    from_kevinton = bool(getattr(deps, "skill_review_required", False))
    requested_by = getattr(deps, "user_id", "") or ""
    with _lock:
        data = _load()
        data[proposal_id] = {
            "spec": spec,
            "description": description,
            "requested_by": requested_by,
            "from_kevinton": from_kevinton,
            "channel_id": getattr(deps, "channel_id", "") or "",
            "created_at": time.time(),
        }
        _save(data)
    who = "kevinton" if from_kevinton else f"<@{requested_by}>"
    summary = f":mag: *Skill change for review* (`{proposal_id}`) from {who}:\n{description}"
    notify_admin(f"Skill change for review from {who}: {description}", blocks=_review_blocks(proposal_id, summary, preview))
    return (
        f"Submitted for review (proposal `{proposal_id}`): {description}. It takes effect once "
        "the coolton maintainer approves it — tell the user it's pending review, not done."
    )


def pop(proposal_id: str) -> dict | None:
    """Remove and return a pending proposal (None if unknown or already handled)."""
    with _lock:
        data = _load()
        proposal = data.pop(proposal_id, None)
        if proposal is not None:
            _save(data)
    return proposal


def discard(proposal: dict) -> None:
    """Clean up a rejected proposal's staged files, if it has any."""
    staged = (proposal.get("spec") or {}).get("staged_dir")
    # Only ever a directory new_staging_dir made: <repo>/skill_proposals/<id>.
    if staged and os.path.basename(os.path.dirname(os.path.abspath(staged))) == STAGING_DIR:
        shutil.rmtree(staged, ignore_errors=True)
