from logging import Logger

from slack_bolt import BoltContext, Say, SayStream
from slack_sdk import WebClient

from agent import AgentDeps, run_agent
from thread_context import conversation_store
from listeners.views.feedback_builder import build_feedback_blocks


def handle_message(
    client: WebClient,
    context: BoltContext,
    event: dict,
    logger: Logger,
    say: Say,
    say_stream: SayStream,
    set_status,  # SetStatus — unused, we call API directly
):
    """Handle messages sent to the agent via DM or in threads the bot is part of."""

    # Skip message subtypes (edits, deletes, etc.) and bot messages.
    if event.get("subtype"):
        return
    if event.get("bot_id"):
        return

    is_dm = event.get("channel_type") == "im"
    is_thread_reply = event.get("thread_ts") is not None

    if is_dm:
        pass
    elif is_thread_reply:
        # Channel thread replies are handled only if the bot is already engaged
        history = conversation_store.get_history(context.channel_id, event["thread_ts"])
        if history is None:
            return
    else:
        # Top-level channel messages are handled by app_mentioned
        return

    try:
        channel_id = context.channel_id
        text = event.get("text", "")
        if text.strip().startswith("##"):
            logger.info(f"Ignoring message starting with '##': {text}")
            return
        thread_ts = event.get("thread_ts") or event["ts"]

        user_id = context.user_id

        # Get conversation history
        history = conversation_store.get_history(channel_id, thread_ts)

        # Set assistant thread status with loading messages
        client.assistant_threads_setStatus(
            channel_id=channel_id,
            thread_ts=thread_ts,
            status="Thinking...",
            loading_messages=[
                "Teaching the hamsters to type faster…",
                "Untangling the internet cables…",
                "Consulting the office goldfish…",
                "Polishing up the response just for you…",
                "Convincing the AI to stop overthinking…",
            ],
        )

        # Run the agent
        deps = AgentDeps(
            client=client,
            user_id=user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            message_ts=event["ts"],
            user_token=context.user_token,
        )

        from agent.plan_block import send_plan_message, finalize_plan_message, complete_plan_message
        plan_ts = send_plan_message(deps)
        deps.plan_ts = plan_ts

        result = run_agent(text, deps, message_history=history)

        if deps.should_skip:
            if plan_ts:
                finalize_plan_message(deps)
                complete_plan_message(deps)
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
        from agent.kevinton import spawn_kevinton

        spawn_kevinton(text, result.all_messages(), channel_id, thread_ts, deps)

    except Exception as e:
        logger.exception(f"Failed to handle message: {e}")
        try:
            from agent.plan_block import set_plan_error
            set_plan_error(deps, str(e))
        except Exception:
            pass
        say(
            text=f":warning: Something went wrong! ({type(e).__name__}: {e})",
            thread_ts=event.get("thread_ts") or event.get("ts"),
        )
