"""Shared turn-execution logic for the message and app_mention handlers.

Both events feed the same pipeline after they've decided to respond:
status set, deps built, plan message posted, agent run, response streamed,
history persisted, kevinton spawned. This keeps that pipeline in one place.
"""

import time
from logging import Logger

from slack_bolt import Say, SayStream
from slack_sdk import WebClient

import agent.thread_status as thread_status
from agent import AgentDeps, run_agent
from agent.redact import redact as _redact
from agent.surface import get_surface as _surface
from thread_context import conversation_store, conversation_trace_store
from listeners.views.feedback_builder import build_feedback_blocks

# chat.postMessage hard-caps text at 40,000 chars; stay under it when chunking a
# response that the streaming API rejected as msg_too_long.
_MAX_MESSAGE_CHARS = 38000

# run_agent_turn recurses into itself (via the `finally` block below) to drain
# a message stranded in the steering queue by the run that's ending. Each
# recursive call is a real stack frame, not a loop — bound how deep that can
# go so a pathological run of stranding (a bug elsewhere re-queuing messages
# faster than turns can drain them, or just enough near-simultaneous messages
# racing the same !stop window) can never grow the stack unboundedly.
_MAX_STRANDED_RECURSION_DEPTH = 25


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
    logger: Logger,
    channel_id: str,
    thread_ts: str,
    message_ts: str,
    user_id: str,
    user_token: str | None,
    text: str,
    history,
    images: list[dict] | None = None,
    say_stream: SayStream | None = None,
    say: Say | None = None,
    surface: object | None = None,
    _stranded_recursion_depth: int = 0,
) -> None:
    """Run one agent turn: status → plan message → run → stream → history → kevinton.

    Exceptions are reported to the thread (mirroring the old per-handler
    error handling) and never propagate.

    Slack callers pass `say_stream`/`say` (Bolt's own response helpers) and leave
    `surface` unset — a SlackSurface is built lazily from `client`/`channel_id`/
    `thread_ts`/`message_ts` the first time something needs it (see
    agent.surface.get_surface). Non-Slack callers (the web UI) pass an explicit
    `surface` and no `say_stream`/`say`; everything downstream — steering, !stop,
    kevinton, history compaction — is the exact same pipeline either way.
    """
    from agent.ban_store import is_banned
    if is_banned(user_id):
        # The single choke point both Slack and the web UI funnel through, so
        # a ban applies everywhere at once rather than needing a check per
        # surface. No plan block, no active_runs tracking — this never starts
        # a real turn at all.
        refusal = "you're banned from using coolton."
        if say:
            say(text=refusal, thread_ts=thread_ts)
        elif surface is not None:
            surface.post_error(refusal)
        return

    # Slack callers either pass an explicit SlackSurface or nothing (the lazy
    # default is also a SlackSurface); a non-Slack caller always passes its own
    # surface explicitly. Decided once, up front, so both the try body and the
    # finally block (thread_status is Slack-only) agree on it.
    is_slack = surface is None or getattr(surface, "name", "slack") == "slack"

    deps = None
    from agent.active_runs import mark_run_finished, mark_run_started
    mark_run_started(channel_id, thread_ts, time.time())
    try:
        from agent.provider_config import extract_tag_directive
        text, tag_filter, tag_error = extract_tag_directive(text)
        if tag_error:
            if say:
                say(text=tag_error, thread_ts=thread_ts)
            elif surface is not None:
                surface.post_error(tag_error)
            return

        if is_slack:
            # Live "what's coolton doing" status pill: starts at "Working", then tracks
            # the last tool called (agent.plan_block's before_tool_execute hook) and
            # refreshes every 30s in between — see agent.thread_status.
            thread_status.start(client, channel_id, thread_ts)

        deps = AgentDeps(
            client=client,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=message_ts,
            user_token=user_token,
            provider_tag_filter=tag_filter,
            surface=surface,
        )
        conv_surface = _surface(deps)

        from agent.plan_block import (
            send_plan_message,
            finalize_plan_message,
            complete_plan_message,
            delete_plan_message,
            set_plan_error,
        )
        # The Slack "plan/thinking" block is Slack-specific (a chat_update'd rich
        # block); a non-Slack surface shows its own live progress through
        # conv_surface.build_hooks() instead (wired into run_agent via
        # deps.surface), so there's no plan_ts to manage here.
        plan_ts = send_plan_message(deps) if is_slack else None
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

            output = _redact(result.output, context="final response")
            if is_slack:
                # Stream response in thread with feedback buttons. If streaming fails
                # (e.g. msg_too_long on chat.startStream), fall back to regular
                # chat.postMessage with the message chunked to fit Slack's limit.
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
            else:
                conv_surface.post_final(output)
            complete_plan_message(deps)

        conv_surface.finish_turn(deps)

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
        error_text = f":warning: Something went wrong! ({type(e).__name__}: {_redact(str(e))})"
        if say:
            say(text=error_text, thread_ts=thread_ts)
        elif surface is not None:
            surface.post_error(error_text)
    finally:
        # Whatever happened, this thread is no longer "actively running" —
        # any message.py/app_mentioned.py check from here on should start a
        # fresh turn rather than queuing as a steer for a run that's over.
        mark_run_finished(channel_id, thread_ts)
        if is_slack:
            thread_status.stop(channel_id, thread_ts)

        # A message can land in the steering queue for a run that's about to end
        # without ever getting the chance to fold it into a live tool result — most
        # commonly the !stop race (stop_requested_for is only checked at the next
        # before_tool_execute call, so a message sent right after !stop can still be
        # queued here for the run that's already dying) but also just a message
        # arriving in the last moment before the run's own final response. Simply
        # discarding it here (the old behavior) silently drops it — the thread then
        # looks like it stopped answering anything at all. Drain and actually answer
        # it with a fresh turn instead.
        from agent.steering_store import clear_steering_messages, peek_steering_messages
        stranded = peek_steering_messages(channel_id, thread_ts)
        clear_steering_messages(channel_id, thread_ts)
        if stranded and _stranded_recursion_depth >= _MAX_STRANDED_RECURSION_DEPTH:
            # Give up recursing rather than risk an unbounded stack — this
            # should never actually happen (it would take that many turns
            # each stranding another message in the narrow window between
            # this turn ending and the queue being drained), but silently
            # dropping the last stranded message is still better than a
            # crash, and it's logged so a real pathological case is visible.
            logger.error(
                "Stranded-steering recursion hit its depth limit (%d) in %s/%s; "
                "dropping the last stranded message instead of recursing further.",
                _MAX_STRANDED_RECURSION_DEPTH, channel_id, thread_ts,
            )
        elif stranded:
            last = stranded[-1]
            run_agent_turn(
                client=client,
                say_stream=say_stream,
                say=say,
                surface=surface,
                logger=logger,
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_ts=last["message_ts"] or thread_ts,
                user_id=last["user_id"],
                user_token=user_token,
                text=last["text"],
                history=conversation_store.get_history(channel_id, thread_ts),
                _stranded_recursion_depth=_stranded_recursion_depth + 1,
            )
