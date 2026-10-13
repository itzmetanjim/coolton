from slack_bolt import App

from listeners import actions, events, views
from listeners.events.shared import register_private_hooks


def register_listeners(app: App):
    # First: actions ends with a catch-all, and Bolt runs only the first listener that matches.
    register_private_hooks(app)
    actions.register(app)
    events.register(app)
    views.register(app)
