import os

from agentmail import AgentMail

AGENTMAIL_KEY_ENV = "AGENTMAIL_API_KEY"
DEFAULT_INBOX = "coolton@agentmail.to"


def _client() -> AgentMail | None:
    key = os.environ.get(AGENTMAIL_KEY_ENV)
    if not key:
        return None
    return AgentMail(api_key=key)


def _err(e: Exception) -> str:
    return f"AgentMail error: {getattr(e, 'message', None) or str(e)}"


def create_inbox_tool() -> str:
    """Create a new AgentMail inbox for coolton (gives coolton its own @agentmail.to address).

    Returns the new inbox id/address. Use this when you need a fresh email identity.
    """
    client = _client()
    if client is None:
        return "Error: AGENTMAIL_API_KEY not configured."
    try:
        inbox = client.inboxes.create()
        inbox_id = getattr(inbox, "inbox_id", None) or getattr(inbox, "id", None)
        email = getattr(inbox, "email", inbox_id)
        return f"Created inbox: {inbox_id} (address: {email})"
    except Exception as e:
        return _err(e)


def list_inboxes_tool(limit: int = 20) -> str:
    """List coolton's AgentMail inboxes.

    Args:
        limit: Max number of inboxes to return (default 20).

    Returns: a list of inbox ids/addresses.
    """
    client = _client()
    if client is None:
        return "Error: AGENTMAIL_API_KEY not configured."
    try:
        resp = client.inboxes.list(limit=limit)
        inboxes = getattr(resp, "inboxes", resp) or []
        if not inboxes:
            return "No AgentMail inboxes yet."
        lines = []
        for ib in inboxes:
            inbox_id = getattr(ib, "inbox_id", None) or getattr(ib, "id", None)
            email = getattr(ib, "email", inbox_id)
            lines.append(f"- {inbox_id} (email: {email})")
        return "\n".join(lines)
    except Exception as e:
        return _err(e)


def list_messages_tool(inbox_id: str = DEFAULT_INBOX, limit: int = 20) -> str:
    """List recent messages in a given AgentMail inbox.

    Args:
        inbox_id: The inbox id or @agentmail.to address (defaults to coolton's inbox).
        limit: Max messages to return (default 20).

    Returns: message ids, subjects, from, and read status.
    """
    client = _client()
    if client is None:
        return "Error: AGENTMAIL_API_KEY not configured."
    try:
        resp = client.inboxes.messages.list(inbox_id, limit=limit)
        msgs = getattr(resp, "messages", resp) or []
        if not msgs:
            return f"No messages in inbox {inbox_id}."
        lines = []
        for m in msgs:
            mid = getattr(m, "message_id", None) or getattr(m, "id", None)
            sender = getattr(m, "from_", None) or getattr(m, "from", "?")
            subject = getattr(m, "subject", "(no subject)")
            read = getattr(m, "read", "?")
            lines.append(
                f"- id={mid} | from={sender} | subject={subject} | read={read}"
            )
        return "\n".join(lines)
    except Exception as e:
        return _err(e)


def read_message_tool(inbox_id: str = DEFAULT_INBOX, message_id: str = "") -> str:
    """Read the full content of a specific AgentMail message.

    Args:
        inbox_id: The inbox id or @agentmail.to address (defaults to coolton's inbox).
        message_id: The message id from list_messages.

    Returns: sender, subject, and the message body text.
    """
    client = _client()
    if client is None:
        return "Error: AGENTMAIL_API_KEY not configured."
    try:
        m = client.inboxes.messages.get(inbox_id, message_id)
        d = m.model_dump() if hasattr(m, "model_dump") else {}
        sender = d.get("from_") or d.get("from") or "?"
        recipient = d.get("to") or "?"
        subject = d.get("subject") or "(no subject)"
        date = d.get("date") or "?"
        body = d.get("text") or d.get("body") or "(no body)"
        parts = [
            f"From: {sender}",
            f"To: {recipient}",
            f"Subject: {subject}",
            f"Date: {date}",
            "",
            str(body),
        ]
        return "\n".join(parts)
    except Exception as e:
        return _err(e)


def send_email_tool(
    to: str,
    subject: str,
    text: str,
    inbox_id: str = DEFAULT_INBOX,
    cc: str = "",
    html: str = "",
) -> str:
    """Send an email from a coolton AgentMail inbox.

    Args:
        to: Recipient email address (or comma-separated list).
        subject: Email subject.
        text: Plain-text body.
        inbox_id: The inbox id or @agentmail.to address to send from (defaults to coolton's inbox).
        cc: Optional CC address(es), comma-separated.
        html: Optional HTML body (used only if text is empty).

    Returns: the sent message id.
    """
    client = _client()
    if client is None:
        return "Error: AGENTMAIL_API_KEY not configured."
    try:
        to_list = [t.strip() for t in to.split(",") if t.strip()]
        cc_list = [c.strip() for c in cc.split(",") if c.strip()] or None
        resp = client.inboxes.messages.send(
            inbox_id,
            to=to_list,
            cc=cc_list,
            subject=subject,
            text=text or None,
            html=html or None,
        )
        return f"Sent. message_id={getattr(resp, 'message_id', '?')}"
    except Exception as e:
        return _err(e)
