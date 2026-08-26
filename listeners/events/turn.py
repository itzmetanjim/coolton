"""Shared turn-execution logic for the message and app_mention handlers.

Both events feed the same pipeline after they've decided to respond:
status set, deps built, plan message posted, agent run, response streamed,
history persisted, kevinton spawned. This keeps that pipeline in one place.
"""

from logging import Logger

from slack_bolt import Say, SayStream
from slack_sdk import WebClient

from agent import AgentDeps, run_agent
from agent.redact import redact as _redact
from thread_context import conversation_store, conversation_trace_store
from listeners.views.feedback_builder import build_feedback_blocks

_LOADING_MESSAGES = [
    "Teaching the hamsters to type faster…",
    "Untangling the internet cables…",
    "Consulting the office goldfish…",
    "Polishing up the response just for you…",
    "Convincing the AI to stop overthinking…",
]

# chat.postMessage hard-caps text at 40,000 chars; stay under it when chunking a
# response that the streaming API rejected as msg_too_long.
_MAX_MESSAGE_CHARS = 38000


def _chunk_text(text: str, limit: int = _MAX_MESSAGE_CHARS) -> list[str]:
    """Split text into chunks of at most `limit` chars, on line boundaries.

    A single over-long line is hard-split at the limit so the total still fits
    Slack's per-message cap.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append("".join(current))
                current, current_len = [], 0
            chunks.extend(line[i : i + limit] for i in range(0, len(line), limit))
            continue
        if current_len + len(line) > limit and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


def _post_fallback_response(
    *, client: WebClient, logger: Logger, channel_id: str, thread_ts: str,
    output: str, feedback_blocks,
) -> None:
    """Deliver a response with regular chat.postMessage when streaming fails.

    Uses markdown_text (not text) so the model's GFM-style output (**bold**,
    [links](url), etc.) actually renders — plain `text` is parsed as Slack's
    own mrkdwn dialect, not standard Markdown, and would show literal
    asterisks/brackets instead of formatting.
    """
    for chunk in _chunk_text(output):
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, markdown_text=chunk)
    try:
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, blocks=feedback_blocks
        )
    except Exception as e:
        logger.warning(f"Failed to post feedback buttons after streaming fallback: {e}")


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
        from agent.provider_config import extract_tag_directive
        text, tag_filter, tag_error = extract_tag_directive(text)
        if tag_error:
            say(text=tag_error, thread_ts=thread_ts)
            return

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
            provider_tag_filter=tag_filter,
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

            # Stream response in thread with feedback buttons. If streaming fails
            # (e.g. msg_too_long on chat.startStream), fall back to regular
            # chat.postMessage with the message chunked to fit Slack's limit.
            output = _redact(result.output, context="final response")
            feedback_blocks = build_feedback_blocks()
            try:
                streamer = say_stream()
                streamer.append(markdown_text=output)
                streamer.stop(blocks=feedback_blocks)
            except Exception as e:
                logger.warning(f"Streaming response failed ({e}); falling back to chat.postMessage")
                _post_fallback_response(
                    client=client,
                    logger=logger,
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    output=output,
                    feedback_blocks=feedback_blocks,
                )
            complete_plan_message(deps)

        # Store conversation history, compacting it first if the thread has run long
        # enough that carrying the full raw history would waste context on every
        # future turn (see agent/history_compaction.py).
        all_messages = result.all_messages()
        try:
            from agent.history_compaction import maybe_compact_history
            stored_messages = maybe_compact_history(all_messages, deps)
        except Exception:
            logger.exception("History compaction failed; storing full history")
            stored_messages = all_messages
        conversation_store.set_history(channel_id, thread_ts, stored_messages)
        try:
            conversation_trace_store.write_from_slack(
                client, channel_id, thread_ts, all_messages
            )
        except Exception:
            logger.exception("Failed to persist conversation training log")

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
            text=f":warning: Something went wrong! ({type(e).__name__}: {_redact(str(e))})",
            thread_ts=thread_ts,
        )
