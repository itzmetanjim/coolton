"""Live turn-progress hooks for the web surface — the web UI's equivalent of
agent.plan_block's Slack "plan/thinking" block, emitting structured JSON step
events into the conversation's event log (web.conversation_log) instead of a
chat_update'd Slack block.

Reuses agent.plan_block's pure helpers (tool display names, leak detection,
steering-note formatting, resume-checkpoint trimming) rather than duplicating
them — only the "how do I show this" half differs between the two surfaces.
"""

from __future__ import annotations

import logging

from agent.plan_block import (
    _display_for_tool,
    _looks_like_tool_call_leakage,
    _messages_safe_for_resume,
    _safe_str,
    _steering_note,
)
from agent.redact import redact as _redact
from agent.steering_store import clear_steering_messages, peek_steering_messages
from agent.stop_store import HaltRun, stop_requested_for

logger = logging.getLogger(__name__)

try:
    from pydantic_ai.capabilities import Hooks
except Exception:  # pragma: no cover
    Hooks = None  # type: ignore


def _redact_json(value):
    """Redact secrets from a JSON-ish value recursively, without collapsing it
    to a flat string first — unlike plan_block's _pretty_args, which exists
    specifically to flatten args for Slack's plain-text card. The web UI can
    render real structure, so it keeps it."""
    if isinstance(value, dict):
        return {k: _redact_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_json(v) for v in value]
    if isinstance(value, str):
        return _redact(value, context="web step")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _redact(_safe_str(value), context="web step")


def build_web_hooks(conversation_id: str):
    from web import conversation_log as log

    hooks = Hooks()

    @hooks.on.after_model_request
    async def after_model(ctx, *, request_context, response):
        # Stop as soon as the model comes back, not only at the next tool call.
        # On Slack a !stop is checked in before_tool_execute alone, which means a
        # turn that has stopped calling tools and is writing its final answer
        # ignores the request entirely. The web UI has a real Stop button sitting
        # right there, so it gets the earlier checkpoint too.
        deps = ctx.deps
        if stop_requested_for(deps.channel_id, deps.thread_ts, deps.run_started_at):
            try:
                deps.halted_messages = _messages_safe_for_resume(ctx.messages)
            except Exception:
                deps.halted_messages = deps.last_attempt_messages
            raise HaltRun("!stop requested")

        has_tool_calls = any(
            getattr(part, "part_kind", None) == "tool-call" for part in response.parts
        )
        if has_tool_calls:
            status_text = "\n\n".join(
                part.content
                for part in response.parts
                if getattr(part, "part_kind", None) == "text" and getattr(part, "content", None)
            ).strip()
            if status_text:
                redacted = _redact(status_text, context="status update")
                if _looks_like_tool_call_leakage(redacted):
                    logger.warning(
                        "WEB STATUS  | dropped (looks like leaked tool-call syntax): %s",
                        redacted[:500],
                    )
                else:
                    log.append_event(conversation_id, {
                        "type": "agent_message", "variant": "status", "text": redacted,
                    })

        thinking_text = "\n\n".join(
            part.content
            for part in response.parts
            if getattr(part, "part_kind", None) == "thinking" and getattr(part, "content", None)
        )
        if thinking_text.strip():
            log.append_event(conversation_id, {
                "type": "step", "kind": "reasoning", "status": "complete",
                "text": _redact(thinking_text, context="model reasoning"),
            })
        return response

    @hooks.on.before_tool_execute
    async def before_tool(ctx, *, call, tool_def, args):
        deps = ctx.deps
        display = _display_for_tool(call.tool_name)
        log.append_event(conversation_id, {"type": "turn_status", "text": f"calling tool: {display}"})

        # Checkpoint how far this attempt has gotten — same resume mechanism the
        # Slack side uses (see AgentDeps.last_attempt_messages).
        safe_messages = _messages_safe_for_resume(ctx.messages)
        deps.last_attempt_messages = safe_messages
        if stop_requested_for(deps.channel_id, deps.thread_ts, deps.run_started_at):
            deps.halted_messages = safe_messages
            raise HaltRun("!stop requested")

        log.append_event(conversation_id, {
            "type": "step", "kind": "tool", "step_id": call.tool_call_id,
            "tool_name": call.tool_name, "display": display,
            "status": "in_progress", "args": _redact_json(args),
        })
        return args

    @hooks.on.after_tool_execute
    async def after_tool(ctx, *, call, tool_def, args, result):
        deps = ctx.deps

        steering = peek_steering_messages(deps.channel_id, deps.thread_ts)
        if steering and isinstance(result, str):
            requester_id = getattr(deps, "user_id", "")
            notes = "\n\n".join(_steering_note(s, requester_id) for s in steering)
            result = f"{result}\n\n{notes}"
            clear_steering_messages(deps.channel_id, deps.thread_ts)

        log.append_event(conversation_id, {
            "type": "step", "kind": "tool", "step_id": call.tool_call_id,
            "tool_name": call.tool_name, "display": _display_for_tool(call.tool_name),
            "status": "complete", "result": _redact_json(_safe_str(result)),
        })
        return result

    @hooks.on.tool_execute_error
    async def on_tool_error(ctx, *, call, tool_def, args, error):
        log.append_event(conversation_id, {
            "type": "step", "kind": "tool", "step_id": call.tool_call_id,
            "tool_name": call.tool_name, "display": _display_for_tool(call.tool_name),
            "status": "error",
            "result": _redact(str(error), context=f"tool error {call.tool_name}"),
        })
        raise error

    return hooks
