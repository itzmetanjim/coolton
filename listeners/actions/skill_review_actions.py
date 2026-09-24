"""Approve / Reject buttons on a skill-change review DM (agent.skill_review).

Only the maintainer can act on them; anyone else's click does nothing. The DM
is rewritten in place with the outcome so it can't be acted on twice, and the
person who asked (not kevinton) hears back about their request.
"""

from logging import Logger

from slack_sdk import WebClient

from agent.admin_alerts import ADMIN_USER_ID


def _resolve(ack, body: dict, client: WebClient, logger: Logger, approve: bool) -> None:
    ack()
    if (body.get("user") or {}).get("id") != ADMIN_USER_ID:
        return
    from agent import skill_review

    proposal_id = body["actions"][0].get("value", "")
    proposal = skill_review.pop(proposal_id)
    if proposal is None:
        outcome = f"Proposal `{proposal_id}` was already handled."
    elif approve:
        from agent.agent import apply_skill_change

        try:
            outcome = apply_skill_change(proposal["spec"])
        except Exception as e:
            logger.exception("Applying skill proposal %s failed", proposal_id)
            skill_review.discard(proposal)
            outcome = f"Error applying the change: {e}"
    else:
        skill_review.discard(proposal)
        outcome = "Rejected — nothing was changed."

    description = (proposal or {}).get("description", "")
    verdict = "approved" if approve else "rejected"
    text = f":mag: Skill change `{proposal_id}` {verdict}: {description}\n{outcome}"
    try:
        client.chat_update(
            channel=body["channel"]["id"], ts=body["message"]["ts"], text=text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": text}}],
        )
    except Exception:
        logger.exception("Couldn't update the review DM for skill proposal %s", proposal_id)

    requester = (proposal or {}).get("requested_by")
    if proposal and requester and not proposal.get("from_kevinton") and requester != ADMIN_USER_ID:
        try:
            client.chat_postMessage(
                channel=requester,
                text=f"Your skill change ({description}) was {verdict} by the coolton maintainer.\n{outcome}",
            )
        except Exception:
            logger.exception("Couldn't tell %s about skill proposal %s", requester, proposal_id)


def handle_skill_review_approve(ack, body: dict, client: WebClient, logger: Logger):
    _resolve(ack, body, client, logger, approve=True)


def handle_skill_review_reject(ack, body: dict, client: WebClient, logger: Logger):
    _resolve(ack, body, client, logger, approve=False)
