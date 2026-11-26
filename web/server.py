"""coolton web UI service — FastAPI app serving the SPA shell + REST/SSE API.

Runs on a daemon thread inside the main coolton process (see app.py), sharing
the in-memory Slack-side stores (agent.active_runs, agent.steering_store,
thread_context.conversation_store, sandbox state) with real Slack turns — see
web/runner.py's own note for why a second process would break that.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from web import auth, bot_oauth, conversation_log as log, conversations

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    log.repair_orphaned_turns(skip_conversation_ids=log.get_resuming_conversation_ids())
    yield


app = FastAPI(title="coolton", lifespan=_lifespan)


@app.middleware("http")
async def _same_origin_writes(request: Request, call_next):
    """Refuse a write a browser sends from another site. SameSite=Lax cookies don't
    stop this from a *.tanjim.org page (that's the same site), and some writes here
    are "simple" requests a page can send without a preflight (a multipart upload, an
    empty POST to stop a turn)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        origin = request.headers.get("origin")
        if origin and urlparse(origin).hostname != auth._request_host(request):
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
    return await call_next(request)


app.include_router(auth.router)
app.include_router(conversations.router)
app.include_router(bot_oauth.router)


@app.get("/")
def index(request: Request):
    # An unauthenticated visit skips straight to login (no flash of app shell)
    # — except landing here right after /oauth/logout or a failed sign-in,
    # where the point is to actually show a page instead of silently bouncing
    # through /oauth/login again (Hack Club Auth keeps its own SSO session, so
    # that redirect alone re-authorizes without the user seeing anything: see
    # /oauth/logout's own comment). The frontend reads these same params to
    # render that state instead of calling any authenticated endpoint.
    landing_from_auth = "signed_out" in request.query_params or "auth_error" in request.query_params
    if not auth.require_slack_id(request) and not landing_from_auth:
        return RedirectResponse("/oauth/login", status_code=302)
    return FileResponse(_STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


def run(host: str = "127.0.0.1", port: int | None = None) -> None:
    import uvicorn

    uvicorn.run(
        app, host=host,
        port=port or int(os.environ.get("COOLTON_WEB_PORT", "34343")),
        log_level="info",
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
