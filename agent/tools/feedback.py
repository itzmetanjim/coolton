"""Agent-callable feedback about coolton itself.

Complements the existing button-triggered flow (a thumbs up/down under a
specific reply — see listeners/views/feedback_builder.py and
listeners/views/feedback_views.py): this is for when someone reports a bug,
praises something, or requests a change conversationally, with no message to
click a button under. Modeled on gorkie's submit_feedback tool.
"""

from agent.admin_alerts import notify_admin

_KINDS = {"bug", "praise", "suggestion", "other"}
_DM_INTERVAL_SECONDS = 5 * 60
_EMOJI = {
    "bug": ":lady_beetle:",
    "praise": ":tada:",
    "suggestion": ":bulb:",
    "other": ":speech_balloon:",
}


def submit_feedback(user_id: str, channel_id: str, kind: str, body: str) -> str:
    kind = (kind or "").strip().lower()
    if kind not in _KINDS:
        return f"Error: kind must be one of {', '.join(sorted(_KINDS))}."
    if not body or not body.strip():
        return "Error: body is required."

    text = (
        f"{_EMOJI.get(kind, ':speech_balloon:')} *Agent-submitted feedback ({kind})* "
        f"from <@{user_id}> in `{channel_id}`:\n{body.strip()}"
    )
    # Every piece is kept (feedback.json); the DM is at most one per person every few
    # minutes, so nobody can flood the maintainer through this tool.
    from agent.feedback_store import record_submitted

    record_submitted(user_id, channel_id, kind, body.strip())
    notify_admin(text, dedupe_key=f"feedback:{user_id}", min_interval_seconds=_DM_INTERVAL_SECONDS)
    return f"Logged {kind} feedback. Thanks!"
