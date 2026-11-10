"""coolton's own email inboxes, through AgentMail (agent.tools.agentmail).

Registered on the agent in agent.agent; nothing here imports agent.agent.
"""

from pydantic_ai import RunContext

from agent.deps import AgentDeps

def agentmail_create_inbox(ctx: RunContext[AgentDeps]) -> str:
    """Create a new AgentMail inbox for coolton (gives coolton its own @agentmail.to address).

    Use when you need a fresh email identity to send/receive mail autonomously.
    """
    from agent.tools.agentmail import create_inbox_tool

    return create_inbox_tool()


def agentmail_list_inboxes(ctx: RunContext[AgentDeps], limit: int = 20) -> str:
    """List coolton's AgentMail inboxes (ids + @agentmail.to addresses)."""
    from agent.tools.agentmail import list_inboxes_tool

    return list_inboxes_tool(limit=limit)


def agentmail_list_messages(ctx: RunContext[AgentDeps], inbox_id: str = "coolton@agentmail.to", limit: int = 20) -> str:
    """List recent messages in a coolton AgentMail inbox.

    Args:
        inbox_id: The inbox id or @agentmail.to address (defaults to coolton@agentmail.to).
        limit: Max messages to return (default 20).
    """
    from agent.tools.agentmail import list_messages_tool

    return list_messages_tool(inbox_id, limit=limit)


def agentmail_read_message(ctx: RunContext[AgentDeps], message_id: str, inbox_id: str = "coolton@agentmail.to") -> str:
    """Read the full content of a specific AgentMail message.

    Args:
        message_id: The message id from agentmail_list_messages.
        inbox_id: The inbox id or @agentmail.to address (defaults to coolton@agentmail.to).
    """
    from agent.tools.agentmail import read_message_tool

    return read_message_tool(inbox_id, message_id)


def agentmail_send_email(
    ctx: RunContext[AgentDeps],
    to: str,
    subject: str,
    text: str,
    inbox_id: str = "coolton@agentmail.to",
    cc: str = "",
    html: str = "",
) -> str:
    """Send an email from a coolton AgentMail inbox.

    Args:
        to: Recipient email address (or comma-separated list).
        subject: Email subject.
        text: Plain-text body.
        inbox_id: The inbox id or @agentmail.to address to send from (defaults to coolton@agentmail.to).
        cc: Optional CC address(es), comma-separated.
        html: Optional HTML body (used only if text is empty).
    """
    from agent.tools.agentmail import send_email_tool

    return send_email_tool(to, subject, text, inbox_id=inbox_id, cc=cc, html=html)
