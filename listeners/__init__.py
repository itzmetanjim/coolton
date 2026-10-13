from slack_bolt import App

from listeners import actions, events, views
from listeners.events.shared import register_private_hooks


def register_listeners(app: App):
    actions.register(app)
    events.register(app)
    views.register(app)
    register_private_hooks(app)
