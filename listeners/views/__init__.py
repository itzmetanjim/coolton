from slack_bolt import App

from .byok_views import handle_byok_add_submit, handle_byok_edit_submit
from .instructions_views import handle_instructions_submit
from .feedback_views import handle_feedback_submit


def register(app: App):
    app.view("byok_add_submit")(handle_byok_add_submit)
    app.view("byok_edit_submit")(handle_byok_edit_submit)
    app.view("custom_instructions_submit")(handle_instructions_submit)
    app.view("feedback_submit")(handle_feedback_submit)
