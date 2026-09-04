import logging
import os
import threading
import time

from dotenv import load_dotenv

# Must run before any `agent`-package import: agent.platforms.slack builds its
# SYSTEM_PROMPT as a module-level f-string that reads COOLTON_BOT_ID/
# COOLTON_USER_ID from os.environ ONCE at import time (see build_context_prompt
# for the per-turn equivalent, which re-reads correctly every time). Loading
# .env after that import would permanently bake in empty ids for the process's
# whole lifetime — the model would then see two contradictory values for its
# own identity in the same prompt, every turn.
load_dotenv(dotenv_path=".env", override=False)

from slack_bolt import App  # noqa: E402
from slack_bolt.adapter.socket_mode import SocketModeHandler  # noqa: E402
from slack_sdk import WebClient  # noqa: E402

from agent import get_model  # noqa: E402
from listeners import register_listeners  # noqa: E402
from agent.scheduler import start_scheduler  # noqa: E402
from agent.redact import set_notifier  # noqa: E402
from agent.token_rotation import start_token_rotation  # noqa: E402

# slack_bolt auto-enables OAuth multi-team mode when these are in the
# environment; they are used only by the separate oauth-server service.
os.environ.pop("SLACK_CLIENT_ID", None)
os.environ.pop("SLACK_CLIENT_SECRET", None)
get_model()  # Fail fast if no AI provider key is configured

# DEBUG floods logs with slack_bolt/urllib3/requests/apscheduler internals AND
# bypasses redact.py's secret scrubbing (that only wraps agent tool I/O, not raw
# library debug output) — default to INFO, opt into DEBUG explicitly.
logging.basicConfig(level=os.environ.get("COOLTON_LOG_LEVEL", "INFO").upper())

app = App(
    token=os.environ.get("SLACK_BOT_TOKEN"),
    client=WebClient(
        base_url=os.environ.get("SLACK_API_URL", "https://slack.com/api"),
        token=os.environ.get("SLACK_BOT_TOKEN"),
    ),
)

TOKEN_LEAK_ALERT_USER = "U0B2VTYER33"
_token_leak_alert_last = 0.0
_token_leak_alert_lock = threading.Lock()


def _notify_token_leak(keys, context):
    global _token_leak_alert_last
    now = time.time()
    with _token_leak_alert_lock:
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

start_token_rotation()
register_listeners(app)
start_scheduler(app)


def _start_web_ui() -> None:
    """coolton's web UI (coolton.tanjim.org), on a daemon thread in this same
    process — NOT a second process. It shares agent.active_runs,
    agent.steering_store, and thread_context.conversation_store in memory with
    the Slack side (see web/runner.py), and thread_context's own store persists
    by writing a snapshot of the ENTIRE conversation store to disk, which a
    second process would race and clobber.
    """
    from web.server import run as run_web_server

    threading.Thread(target=run_web_server, daemon=True, name="coolton-web-ui").start()


_start_web_ui()

if __name__ == "__main__":
    SocketModeHandler(app, os.environ.get("SLACK_APP_TOKEN")).start()
