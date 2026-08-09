import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
ERROR_LOG_PATH = BASE_DIR / "oauth_errors.log"
TOKEN_URL = "https://slack.com/api/oauth.v2.access"
PORT = 9052

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("oauth_server")

app = FastAPI(title="coolton OAuth install handler")


def _redirect_uri() -> str:
    return os.environ.get(
        "SLACK_REDIRECT_URI",
        "https://8052.proxy.tanjim.org/slack/oauth_redirect",
    )


def _log_error(payload: dict) -> None:
    entry = {"ts": datetime.now(timezone.utc).isoformat(), **payload}
    try:
        with open(ERROR_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        logger.exception("Failed to write %s", ERROR_LOG_PATH)


def _exchange(code: str) -> dict:
    data = {
        "client_id": os.environ.get("SLACK_CLIENT_ID", ""),
        "client_secret": os.environ.get("SLACK_CLIENT_SECRET", ""),
        "code": code,
        "redirect_uri": _redirect_uri(),
    }
    try:
        resp = requests.post(TOKEN_URL, data=data, timeout=30)
        return resp.json()
    except Exception as e:
        logger.exception("oauth.v2.access request failed")
        return {"ok": False, "error": f"request_failed: {e}"}


def _update_env(bot_token: str, user_token: str) -> int:
    lines = ENV_PATH.read_text().splitlines(keepends=True)
    changed = 0
    for i, line in enumerate(lines):
        if line.startswith("SLACK_BOT_TOKEN="):
            lines[i] = f"SLACK_BOT_TOKEN={bot_token}\n"
            changed += 1
        elif line.startswith("SLACK_USER_TOKEN="):
            lines[i] = f"SLACK_USER_TOKEN={user_token}\n"
            changed += 1
    tmp = ENV_PATH.with_name(".env.tmp")
    tmp.write_text("".join(lines))
    os.replace(tmp, ENV_PATH)
    return changed


def _restart_coolton(bot_user_id: str = "") -> None:
    # Detached + delayed: oauth-server is PartOf coolton.service, so restarting
    # coolton also restarts this unit. Return the HTTP response first, then run
    # the membership sync (best-effort) and restart a second later so the
    # handler isn't killed mid-response. `;` keeps the restart from being
    # skipped if the sync exits non-zero.
    venv_python = BASE_DIR / ".venv" / "bin" / "python"
    cmd = (
        f"sleep 1 && {venv_python} {BASE_DIR / 'oauth_sync.py'} {bot_user_id}; "
        f"sudo -n systemctl restart coolton.service"
    )
    subprocess.Popen(
        ["bash", "-c", cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    logger.info("Scheduled membership sync + coolton.service restart")


def _authorize_url() -> str:
    manifest = json.loads((BASE_DIR / "manifest.json").read_text())
    scopes = manifest["oauth_config"]["scopes"]
    params = {
        "client_id": os.environ.get("SLACK_CLIENT_ID", ""),
        "scope": " ".join(scopes["bot"]),
        "user_scope": " ".join(scopes["user"]),
        "redirect_uri": _redirect_uri(),
    }
    return "https://slack.com/oauth/v2/authorize?" + urlencode(params)


@app.get("/")
def index():
    return RedirectResponse(_authorize_url(), status_code=302)


@app.get("/health")
def health():
    return {"ok": True, "service": "coolton oauth install handler"}


@app.api_route("/slack/events", methods=["GET", "POST"])
async def slack_events(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = None
    # Slack URL-verification handshake for the event subscription request_url.
    if isinstance(payload, dict) and payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge", "")}
    return {"ok": True}


@app.get("/slack/oauth_redirect")
def oauth_redirect(request: Request, code: str = Query(""), state: str = Query("")):
    if not code:
        _log_error({"event": "missing_code", "state": state})
        return HTMLResponse("<h3>Missing code param</h3>", status_code=400)

    result = _exchange(code)

    if not result.get("ok"):
        _log_error(
            {
                "event": "oauth_failed",
                "code_preview": code[:20],
                "state": state,
                "response": result,
            }
        )
        logger.error("oauth.v2.access failed: %s", result.get("error"))
        return HTMLResponse(
            f"<h3>OAuth failed: {result.get('error')}</h3>", status_code=400
        )

    authed_user_id = (result.get("authed_user") or {}).get("id")
    expected = os.environ.get("COOLTON_USER_ID", "")

    if authed_user_id != expected:
        _log_error(
            {
                "event": "unexpected_user",
                "authed_user_id": authed_user_id,
                "expected": expected,
                "state": state,
                "response": result,
            }
        )
        logger.warning(
            "OAuth completed by unexpected user %s (expected %s); tokens NOT updated",
            authed_user_id,
            expected,
        )
        return HTMLResponse(
            f"<h3>Unauthorized installer ({authed_user_id}). Tokens NOT updated.</h3>",
            status_code=403,
        )

    bot_token = result["access_token"]
    user_token = result["authed_user"]["access_token"]
    bot_user_id = result.get("bot_user_id", "")

    try:
        changed = _update_env(bot_token, user_token)
    except Exception as e:
        _log_error({"event": "env_update_failed", "error": str(e), "response": result})
        logger.exception("Failed to update .env")
        return HTMLResponse(f"<h3>Failed to update .env: {e}</h3>", status_code=500)

    _restart_coolton(bot_user_id)
    logger.info("Reinstalled by cooltonUser: .env updated (%s lines), restart scheduled", changed)
    return HTMLResponse(
        "<h3>✅ coolton reinstalled. Tokens updated and service restarting.</h3>"
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
