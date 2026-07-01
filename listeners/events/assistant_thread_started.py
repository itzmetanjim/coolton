from logging import Logger

from slack_bolt.context.set_suggested_prompts import SetSuggestedPrompts

SUGGESTED_PROMPTS = [
    {"title": "Search the web", "message": "Search the web for the latest on AI agents"},
    {"title": "Generate an image", "message": "Generate an image of a mountain lake at sunset"},
    {"title": "Write code", "message": "Write a Python script to fetch data from an API"},
    {"title": "Summarize", "message": "Can you summarize the current thread?"},
]


def handle_assistant_thread_started(
    set_suggested_prompts: SetSuggestedPrompts, logger: Logger
):
    """Handle assistant thread started events by setting suggested prompts."""
    try:
        set_suggested_prompts(
            prompts=SUGGESTED_PROMPTS,
            title="How can I help you today?",
        )
    except Exception as e:
        logger.exception(f"Failed to handle assistant thread started: {e}")
