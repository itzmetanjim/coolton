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

import os
import shutil
import threading
import time
import uuid

from agent.admin_alerts import ADMIN_USER_ID, notify_admin
from agent.json_file import load_json, save_json

PROPOSALS_FILE = "skill_proposals.json"
STAGING_DIR = "skill_proposals"
_PREVIEW_CHARS = 2500

_lock = threading.Lock()


def auto_accept_enabled() -> bool:
    """AUTO_ACCEPT_SKILLS (off unless set truthy): apply every skill change
    immediately, including other users' and kevinton's, instead of queueing
    it for review. The maintainer still gets an FYI DM for each one that
    would otherwise have been reviewed (see note_auto_accepted)."""
    return os.environ.get("AUTO_ACCEPT_SKILLS", "").strip().lower() in ("true", "1", "yes", "on")


def _review_policy_applies(deps) -> bool:
    return bool(getattr(deps, "skill_review_required", False)) or getattr(deps, "user_id", "") != ADMIN_USER_ID


def needs_review(deps) -> bool:
    return _review_policy_applies(deps) and not auto_accept_enabled()


def skip_reason(deps) -> str:
    """Why a change that didn't go to review skipped it — for the tool result."""
    return "AUTO_ACCEPT_SKILLS is on" if _review_policy_applies(deps) else "the maintainer asked for it directly"


def note_auto_accepted(deps, description: str) -> None:
    """FYI DM to the maintainer for a change AUTO_ACCEPT_SKILLS let through
    that would otherwise have waited for their review."""
    if not (_review_policy_applies(deps) and auto_accept_enabled()):
        return
    who = "kevinton" if getattr(deps, "skill_review_required", False) else f"<@{getattr(deps, 'user_id', '') or '?'}>"
    notify_admin(f":mag: Skill change auto-accepted (AUTO_ACCEPT_SKILLS) from {who}: {description}")


def new_staging_dir(repo_root: str) -> str:
    path = os.path.join(os.path.abspath(repo_root), STAGING_DIR, uuid.uuid4().hex[:12])
    os.makedirs(path, exist_ok=True)
    return path


def _load() -> dict:
    return load_json(PROPOSALS_FILE)


def _save(data: dict) -> None:
    save_json(PROPOSALS_FILE, data)


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


# Pending proposals one person (or kevinton) may have waiting at once: each is a DM to
# the maintainer, and an install's staged files on disk.
MAX_PENDING_PER_REQUESTER = 5


def submit(spec: dict, deps, description: str, preview: str = "") -> str:
    """Store a proposed skill change, DM it to the maintainer for review, and
    return the message for the model to relay."""
    proposal_id = uuid.uuid4().hex[:10]
    from_kevinton = bool(getattr(deps, "skill_review_required", False))
    requested_by = getattr(deps, "user_id", "") or ""
    with _lock:
        data = _load()
        pending = [p for p in data.values()
                   if p.get("from_kevinton") == from_kevinton and (from_kevinton or p.get("requested_by") == requested_by)]
        if len(pending) >= MAX_PENDING_PER_REQUESTER:
            discard({"spec": spec})
            return (f"Not submitted: {MAX_PENDING_PER_REQUESTER} skill changes from "
                    f"{'kevinton' if from_kevinton else 'this person'} are already waiting for the maintainer's "
                    "review. Tell the user to wait for those to be approved or rejected first.")
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
