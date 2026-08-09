"""Shared turn-execution logic for the message and app_mention handlers.

Both events feed the same pipeline after they've decided to respond:
status set, deps built, plan message posted, agent run, response streamed,
history persisted, kevinton spawned. This keeps that pipeline in one place.
"""

from logging import Logger

from slack_bolt import Say, SayStream
from slack_sdk import WebClient

from agent import AgentDeps, run_agent
from thread_context import conversation_store
from listeners.views.feedback_builder import build_feedback_blocks

_LOADING_MESSAGES = [
    "Teaching the hamsters to type faster…",
    "Untangling the internet cables…",
    "Consulting the office goldfish…",
    "Polishing up the response just for you…",
    "Convincing the AI to stop overthinking…",
]


def run_agent_turn(
    *,
    client: WebClient,
    say_stream: SayStream,
    say: Say,
    logger: Logger,
    channel_id: str,
    thread_ts: str,
    message_ts: str,
    user_id: str,
    user_token: str | None,
    text: str,
    history,
    images: list[dict] | None = None,
) -> None:
    """Run one agent turn: status → plan message → run → stream → history → kevinton.

    Exceptions are reported to the thread (mirroring the old per-handler
    error handling) and never propagate.
    """
    deps = None
    try:
        client.assistant_threads_setStatus(
            channel_id=channel_id,
            thread_ts=thread_ts,
            status="Thinking...",
            loading_messages=_LOADING_MESSAGES,
        )

        deps = AgentDeps(
            client=client,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            user_token=user_token,
        )

        from agent.plan_block import (
            send_plan_message,
            finalize_plan_message,
            complete_plan_message,
            delete_plan_message,
            set_plan_error,
        )
        plan_ts = send_plan_message(deps)
        deps.plan_ts = plan_ts

        result = run_agent(text, deps, message_history=history, images=images)

        if deps.should_skip:
            if "!stop" in deps.halt_reason:
                # Keep the plan/thinking block and end it in an error state so
                # the user can see the turn was stopped — don't delete it.
                set_plan_error(deps, "coolton has been manually stopped")
            elif plan_ts:
                delete_plan_message(deps)
        else:
            finalize_plan_message(deps, result.output)

            # Stream response in thread with feedback buttons
            streamer = say_stream()
            streamer.append(markdown_text=result.output)
            feedback_blocks = build_feedback_blocks()
            streamer.stop(blocks=feedback_blocks)
            complete_plan_message(deps)

        # Store conversation history
        conversation_store.set_history(channel_id, thread_ts, result.all_messages())

        # kevinton: silent background skill-capture agent (runs after every turn)
        if not deps.should_skip:
            from agent.kevinton import spawn_kevinton

            spawn_kevinton(text, result.all_messages(), channel_id, thread_ts, deps)

    except Exception as e:
        logger.exception(f"Failed to handle message: {e}")
        try:
            if deps is not None:
                from agent.plan_block import set_plan_error
                set_plan_error(deps, str(e))
        except Exception:
            pass
        say(
            text=f":warning: Something went wrong! ({type(e).__name__}: {e})",
            thread_ts=thread_ts,
        )
