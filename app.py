import logging
import os

from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient

from agent import get_model
from listeners import register_listeners
from agent.scheduler import start_scheduler

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

register_listeners(app)
start_scheduler(app)

if __name__ == "__main__":
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()
