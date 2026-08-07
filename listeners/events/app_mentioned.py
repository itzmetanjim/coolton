import re
from logging import Logger

from slack_bolt import BoltContext, Say, SayStream
from slack_sdk import WebClient

from agent import AgentDeps, run_agent
from agent.ensure_coolton_user import ensure_coolton_user_in_channel
from agent.leave_thread_store import rejoin_thread
from agent.stop_store import request_stop
from thread_context import conversation_store
from listeners.views.feedback_builder import build_feedback_blocks


def handle_app_mentioned(
    client: WebClient,
    context: BoltContext,
    event: dict,
    logger: Logger,
    say: Say,
    say_stream: SayStream,
    set_status,  # SetStatus — unused, we call API directly
):
    """Handle @mentions in channels."""
    try:
        channel_id = context.channel_id
        text = event.get("text", "")
        if text.strip().startswith("##"):
            logger.info(f"Ignoring message starting with '##': {text}")
            return
        thread_ts = event.get("thread_ts") or event["ts"]
        user_id = context.user_id

        # !stop: immediately halt every coolton run this user has going.
        if "!stop" in text:
            request_stop(user_id)
            say(
                text="⏹️ stopping all your running coolton instances…",
                thread_ts=thread_ts,
            )
            return

        # Silently make sure cooltonUser is a member of this channel (not in DMs).
        if event.get("channel_type") != "im":
            ensure_coolton_user_in_channel(client, channel_id)

        # A direct mention re-engages us in a previously left thread.
        rejoin_thread(channel_id, thread_ts)

        # The bot mention stays in the text verbatim — the model is taught to read
        # <@BOTID> as "@coolton". Only use a stripped copy to test for empty pings.
        has_content = re.sub(r"<@[A-Z0-9]+>", "", text).strip()

        if not has_content:
            say(
                text="Hey there! How can I help you? Ask me anything and I'll do my best.",
                thread_ts=thread_ts,
            )
            return

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

        # Get conversation history
        history = conversation_store.get_history(channel_id, thread_ts)

        # Mentioned in a thread we've never been part of: pull in the earlier
        # Slack messages so the model has the conversation's context.
        if history is None and event.get("thread_ts"):
            from thread_context.thread_history import build_thread_context

            history = build_thread_context(
                client, channel_id, thread_ts, exclude_ts=event["ts"]
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

        from agent.plan_block import send_plan_message, finalize_plan_message, complete_plan_message, delete_plan_message
        plan_ts = send_plan_message(deps)
        deps.plan_ts = plan_ts

        result = run_agent(text, deps, message_history=history)

        if deps.should_skip:
            if plan_ts:
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
        logger.exception(f"Failed to handle app mention: {e}")
        try:
            from agent.plan_block import set_plan_error
            set_plan_error(deps, str(e))
        except Exception:
            pass
        say(
            text=f":warning: Something went wrong! ({type(e).__name__}: {e})",
            thread_ts=event.get("thread_ts") or event["ts"],
        )
