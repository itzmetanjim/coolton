"""Reporting abuse of coolton to its maintainer (report_abuse_tool).

The model calls report_abuse_tool when a request is NSFW or NSFW-adjacent, uses
coolton to spam people or channels, or otherwise abuses it, and when someone has
found a security hole in coolton. Jev (agent.tool_preload) checks every message
for the same things and, when it flags one, the turn is told to check and report
it (agent.agent.run_agent); Jev alone never DMs the maintainer.

A report DMs the maintainer (agent.admin_alerts.notify_admin) the flagged message,
who sent it and a link to it, so they can find the turn in the logs. NSFW, spam
and "other" reports also stop the request: every tool call after the report this
turn is refused (agent.agent's tool hook, via stopped_tool_call), except the ones
needed to decline. A vulnerability report doesn't stop anything: coolton reports it
once and carries on.
"""
import logging

logger = logging.getLogger(__name__)

CATEGORIES = {
    "nsfw": "NSFW or NSFW-adjacent content",
    "spam": "using coolton to spam people or channels",
    "vulnerability": "a security vulnerability in coolton",
    "other": "other abuse of coolton",
}
# Reporting one of these stops the request; a vulnerability report doesn't.
STOPS_THE_REQUEST = {"nsfw", "spam", "other"}
# What a stopped turn may still do: react, report, and end the turn.
ALLOWED_AFTER_STOP = {"add_emoji_reaction", "report_abuse_tool", "skip"}

_MESSAGE_CHARS = 1500


def _link(deps) -> str:
    """Where the flagged message is: a Slack permalink when there is one, and the
    raw ids either way (for finding the turn in the logs)."""
    ids = f"channel `{deps.channel_id}`, thread `{deps.thread_ts}`, message `{deps.message_ts}`"
    try:
        from web.runner import WEB_CHANNEL_ID

        if deps.channel_id == WEB_CHANNEL_ID:
            return f"web conversation `{deps.thread_ts}` ({ids})"
    except Exception:
        pass
    try:
        permalink = deps.client.chat_getPermalink(channel=deps.channel_id, message_ts=deps.message_ts)["permalink"]
        return f"<{permalink}|the message> ({ids})"
    except Exception:
        return ids


def report(deps, category: str, reason: str) -> str:
    """DM the maintainer about this turn's message. Returns what the model should do next."""
    from agent.admin_alerts import notify_admin
    from agent.redact import redact

    category = (category or "").strip().lower()
    if category not in CATEGORIES:
        return f"Error: category must be one of {', '.join(CATEGORIES)}."
    reported = deps.abuse_reported
    if category not in reported:
        reported.add(category)
        message = redact((deps.request_text or "")[:_MESSAGE_CHARS], context="abuse report")
        quoted = "\n".join(f"> {line}" for line in message.splitlines()) or "> (no text)"
        notify_admin(
            f":rotating_light: *coolton abuse report: {CATEGORIES[category]}*\n"
            f"*From:* <@{deps.user_id}> (`{deps.user_id}`)\n"
            f"*Where:* {_link(deps)}\n"
            f"*Coolton's note:* {redact(reason or '(none)', context='abuse report')}\n"
            f"*Flagged message:*\n{quoted}"
        )
        logger.warning("Abuse report (%s) from %s in %s/%s: %s", category, deps.user_id,
                       deps.channel_id, deps.thread_ts, reason)
    if category in STOPS_THE_REQUEST:
        deps.abuse_stop = category
        return ("Reported to coolton's maintainer. Stop working on this request now: no more tools for it. "
                "Reply briefly that you won't do it, in your usual style, and end your turn.")
    return "Reported to coolton's maintainer. Carry on with what you were doing; don't report this again."


def stopped_tool_call(deps, tool_name: str) -> str | None:
    """The refusal for a tool called after this turn's request was reported and stopped,
    or None if the call may go ahead."""
    category = getattr(deps, "abuse_stop", "")
    if not category or tool_name in ALLOWED_AFTER_STOP:
        return None
    return (f"Refused: this request was reported as {CATEGORIES[category]} and stopped. "
            "Don't continue it: reply briefly that you won't do it and end your turn.")
