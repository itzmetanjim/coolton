import os
from logging import Logger
from urllib.parse import urljoin

from slack_bolt import BoltContext
from slack_sdk import WebClient

from listeners.views.app_home_builder import build_app_home_view
from agent.byok_store import get_user_endpoints, get_text_endpoint_id, get_image_endpoint_id
from listeners.actions.instructions_actions import get_user_instructions
from agent.scheduler import _load_reminders


def handle_app_home_opened(client: WebClient, context: BoltContext, logger: Logger):
    try:
        user_id = context.user_id
        install_url = None
        is_connected = False

        if os.environ.get("SLACK_CLIENT_ID") or os.environ.get("SLACK_USER_TOKEN"):
            if context.user_token or os.environ.get("SLACK_USER_TOKEN"):
                is_connected = True
            else:
                redirect_uri = os.environ.get("SLACK_REDIRECT_URI", "")
                install_url = urljoin(redirect_uri, "/slack/install")

        endpoints = get_user_endpoints(user_id)
        text_ep = get_text_endpoint_id(user_id)
        image_ep = get_image_endpoint_id(user_id)
        has_instructions = bool(get_user_instructions(user_id))
        
        # Load user's reminders
        all_reminders = _load_reminders().get("reminders", [])
        user_reminders = [r for r in all_reminders if r["user_id"] == user_id]

        view = build_app_home_view(
            install_url=install_url,
            is_connected=is_connected,
            endpoints=endpoints,
            text_endpoint_id=text_ep,
            image_endpoint_id=image_ep,
            has_instructions=has_instructions,
            reminders=user_reminders,
        )
        client.views_publish(user_id=user_id, view=view)
    except Exception as e:
        logger.exception(f"Failed to publish App Home: {e}")
