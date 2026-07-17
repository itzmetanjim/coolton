import re
from slack_bolt import App

from .feedback_buttons import handle_feedback_button
from .byok_actions import (
    handle_byok_add,
    handle_byok_select_text,
    handle_byok_select_image,
    byok_edit_pattern,
    byok_delete_pattern,
)
from .instructions_actions import (
    handle_instructions_open,
    handle_instructions_clear,
)
from .fallback_cache_actions import handle_fallback_cache_clear
from .test_providers import handle_test_providers


def register(app: App):
    app.action("feedback")(handle_feedback_button)
    app.action("byok_add")(handle_byok_add)
    app.action("byok_select_text")(handle_byok_select_text)
    app.action("byok_select_image")(handle_byok_select_image)
    app.action(re.compile(r"^byok_edit_(.+)$"))(byok_edit_pattern)
    app.action(re.compile(r"^byok_delete_(.+)$"))(byok_delete_pattern)
    app.action("instructions_open")(handle_instructions_open)
    app.action("instructions_clear")(handle_instructions_clear)
    app.action("fallback_cache_clear")(handle_fallback_cache_clear)
    app.action("test_providers")(handle_test_providers)
