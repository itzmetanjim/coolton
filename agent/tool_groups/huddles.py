"""Slack huddles: playing music through HuddleFM, and coolton joining a huddle itself to
talk (agent.huddle).

Registered on the agent in agent.agent; nothing here imports agent.agent.
"""

import re

from pydantic_ai import RunContext

from agent.deps import AgentDeps
from agent.tool_args import parse_json_object_param

def huddlefm_request_control_tool(
    ctx: RunContext[AgentDeps], channel: str, permissions: str, events: str = "",
) -> str:
    """Request DJ control of a HuddleFM listening session (music), by DMing HuddleFM
    (coolton is allowlisted for this). This doesn't join the huddle: to join one yourself
    and talk, use join_huddle_tool. Call this ONCE per session before using
    huddlefm_command_tool; load the `huddlefm-dj` skill first for the
    permission ids and full command reference.

    There is no immediate success reply: the session host gets an approval
    prompt in Slack and can take up to 5 minutes to respond (or never respond).
    This call only reports an immediate error (e.g. an invalid channel); after
    it returns with no error, tell the user you're waiting on the host and
    only try huddlefm_command_tool again once they say it's approved.

    Args:
        channel: The huddle's source channel, controls channel, or companion
            channel, required, and it must be one of those three.
        permissions: Comma-separated permission ids, e.g. "add,skip,pause,volume".
        events: Comma-separated event subscription names (optional, usually
            leave empty; coolton polls with `status` instead of subscribing).
    """
    from agent.tools.huddlefm import request_control
    return request_control(ctx.deps.client, channel, permissions, events)


def huddlefm_command_tool(
    ctx: RunContext[AgentDeps], command_type: str, channel: str = "", fields: str = "",
) -> str:
    """Send a HuddleFM DJ command (after huddlefm_request_control_tool has been
    approved for the needed permission) and return its JSON reply.

    Args:
        command_type: One of: status, search, add, remove, move, clear, skip,
            previous, toggle, pause, resume, seek, volume, settings, end,
            release_control. The `huddlefm-dj` skill says which permission each
            needs and its extra fields.
        channel: The session's channel, only required if you hold grants on
            more than one HuddleFM session at once.
        fields: JSON object string of the command's extra fields, e.g.
            '{"query": "phonk"}' for search, '{"reference": "..."}' for add,
            '{"percent": 40}' for volume. Leave empty for commands that take
            none (status, clear, skip, previous, toggle, pause, resume, end,
            release_control).
    """
    parsed_fields, parse_error = parse_json_object_param("fields", fields)
    if parse_error:
        return parse_error
    from agent.tools.huddlefm import send_command
    return send_command(ctx.deps.client, command_type, channel, parsed_fields)


def join_huddle_tool(ctx: RunContext[AgentDeps], channel: str = "") -> str:
    """Join a Slack huddle (a voice call) as a participant, or start one: coolton then
    listens, talks back, answers the huddle's thread, and shows its desktop as its video
    while using it.

    Without `channel`, joins this conversation's huddle: the one this thread belongs to, or
    else the channel's ongoing one; if no huddle is going here, it starts one and joins it
    (not in DMs). With `channel`, joins the ongoing huddle of that other channel (a public
    one; it never starts one there). Use it when someone asks you to join, hop on, come to,
    start or open a huddle or call to talk. Not for music: playing songs is HuddleFM
    (huddlefm_* tools), which this has nothing to do with. Joining a huddle that's already
    going first asks the huddle (in its thread) to approve; coolton joins once someone there
    does. Either way a separate voice agent then handles the huddle and its thread, so just
    tell the user briefly.

    Args:
        channel: Another channel whose huddle to join: its id, <#C...> mention or #name.
            Leave empty for this conversation's huddle.
    """
    from agent import huddle

    target = _other_huddle_channel(ctx, channel)
    if target.startswith("Error"):
        return target
    here = target == ctx.deps.channel_id
    return huddle.request_join(
        ctx.deps.client, channel_id=target, thread_ts=ctx.deps.thread_ts if here else None,
        requester_id=ctx.deps.user_id, request_text=ctx.deps.request_text, may_start=here,
    )


def _other_huddle_channel(ctx: RunContext[AgentDeps], channel: str) -> str:
    """The channel a huddle tool acts on: this conversation's, or another one named. Another
    channel's huddle must be in a public channel, which anyone in the workspace may join, so
    nobody can use coolton to get into a private call. Returns the id or an "Error: ..." line."""
    if not (channel or "").strip():
        return ctx.deps.channel_id
    from agent.tools.slack_info import _resolve_channel_id
    from agent.tools.slack_search import assert_readable_channel

    target = _resolve_channel_id(channel.strip())
    if not re.match(r"^[CDG][A-Z0-9]+$", target):
        return f"Error: couldn't find the channel {channel!r}."
    refused = assert_readable_channel(target, ctx.deps.channel_id)
    if refused:
        return f"Error: you only join huddles in public channels (or this conversation's): {refused}"
    return target


def leave_huddle_tool(ctx: RunContext[AgentDeps]) -> str:
    """Leave the Slack huddle coolton is in in this channel (this thread's, or else the
    channel's), or cancel a request to join one that's still waiting. Use it when someone
    asks you to leave, hang up or get off the huddle or call. Only this channel's: anyone
    in another channel's huddle can type `!kick-coolton` in its thread. Nothing to do with
    HuddleFM.
    """
    from agent import huddle

    return huddle.leave(ctx.deps.channel_id, ctx.deps.thread_ts, ctx.deps.user_id)
