import logging
import os
import threading
import time

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from agent import get_model
from listeners import register_listeners
from agent.scheduler import start_scheduler
from agent.redact import set_notifier

load_dotenv(dotenv_path=".env", override=False)
# slack_bolt auto-enables OAuth multi-team mode when these are in the
# environment; they are used only by the separate oauth-server service.
os.environ.pop("SLACK_CLIENT_ID", None)
os.environ.pop("SLACK_CLIENT_SECRET", None)
get_model()  # Fail fast if no AI provider key is configured

logging.basicConfig(level=logging.DEBUG)

app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    client=WebClient(
        base_url=os.environ.get("SLACK_API_URL", "https://slack.com/api"),
        token=os.environ.get("SLACK_BOT_TOKEN"),
    ),
)

TOKEN_LEAK_ALERT_USER = "U0B2VTYER33"
_token_leak_alert_last = 0.0


def _notify_token_leak(keys, context):
    global _token_leak_alert_last
    now = time.time()
    if now - _token_leak_alert_last < 60:
        return
    _token_leak_alert_last = now

    def _send():
        try:
            app.client.chat_postMessage(
                channel=TOKEN_LEAK_ALERT_USER,
                text=(
                    f"⚠️ A secret token appeared in coolton output and was redacted.\n"
                    f"Keys: {', '.join(keys)}\nContext: {context or 'unknown'}"
                ),
            )
        except Exception:
            logging.getLogger(__name__).exception("Failed to send token-leak alert DM")

    threading.Thread(target=_send, daemon=True).start()


set_notifier(_notify_token_leak)

register_listeners(app)
start_scheduler(app)

if __name__ == "__main__":
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()
