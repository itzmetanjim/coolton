import re
from logging import Logger

from slack_bolt import BoltContext, Say, SayStream
from slack_sdk import WebClient

from agent import AgentDeps, run_agent
from agent.word_filter import filter_bad_words
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

        # Strip the bot mention from the text
        cleaned_text = re.sub(r"<@[A-Z0-9]+>", "", text).strip()

        if not cleaned_text:
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

        result = run_agent(cleaned_text, deps, message_history=history)

        if deps.should_skip:
            if plan_ts:
                finalize_plan_message(deps)
                complete_plan_message(deps)
        else:
            # Filter bad words from the agent's output before sending
            safe_output = filter_bad_words(result.output)
            finalize_plan_message(deps, safe_output)

            # Stream response in thread with feedback buttons
            streamer = say_stream()
            streamer.append(markdown_text=safe_output)
            feedback_blocks = build_feedback_blocks()
            streamer.stop(blocks=feedback_blocks)
            complete_plan_message(deps)

        # Store conversation history
        conversation_store.set_history(channel_id, thread_ts, result.all_messages())

        # kevinton: silent background skill-capture agent (runs after every turn)
        from agent.kevinton import spawn_kevinton

        spawn_kevinton(cleaned_text, result.all_messages(), channel_id, thread_ts, deps)

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
