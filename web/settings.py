"""Settings API for coolton's web UI — the Slack App Home controls, surfaced as
REST routes so the web settings popup can do everything App Home does: policy
consent, BYOK endpoints, custom instructions, reminders, the AI fallback cache,
and per-user MCP servers. Every route reuses the exact stores and validation the
Slack action handlers use (listeners/actions/*), so a change made from either
surface is immediately visible in the other.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from web.auth import require_slack_id

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/settings")


def _require_user(request: Request) -> str:
    user_id = require_slack_id(request)
    if not user_id:
        raise HTTPException(401, "Not signed in")
    return user_id


def _bot_client():
    from slack_sdk import WebClient

    return WebClient(token=os.environ.get("SLACK_BOT_TOKEN", ""))


# ---------------------------------------------------------------------------
# Aggregate view
# ---------------------------------------------------------------------------


@router.get("")
def get_settings(request: Request):
    user_id = _require_user(request)

    from agent.agent import _get_user_display_info
    from agent.admin_alerts import is_admin
    from agent.byok_store import get_image_endpoint_id, get_text_endpoint_id, get_user_endpoints
    from agent.mcp_server_store import get_user_servers
    from agent.policy_consent import POLICY_URL, has_consent
    from agent.scheduler import _load_reminders
    from agent.version import COMMIT_SHORT_HASH, COMMIT_URL
    from listeners.actions.instructions_actions import get_user_instructions

    name, _pfp = _get_user_display_info(user_id)
    all_reminders = _load_reminders().get("reminders", [])
    user_reminders = [r for r in all_reminders if r.get("user_id") == user_id]

    return {
        "slack_id": user_id,
        "display_name": name or user_id,
        "avatar_url": f"https://cachet.dunkirk.sh/users/{user_id}/r",
        "policy": {"consent": has_consent(user_id), "url": POLICY_URL},
        "endpoints": get_user_endpoints(user_id),
        "text_endpoint_id": get_text_endpoint_id(user_id),
        "image_endpoint_id": get_image_endpoint_id(user_id),
        "instructions": get_user_instructions(user_id),
        "reminders": user_reminders,
        "mcp_servers": get_user_servers(user_id),
        "mcp_is_connected": bool(os.environ.get("SLACK_USER_TOKEN") or os.environ.get("SLACK_CLIENT_ID")),
        "is_admin": is_admin(user_id),
        "commit": {"short_hash": COMMIT_SHORT_HASH, "url": COMMIT_URL},
    }


# ---------------------------------------------------------------------------
# Policy consent
# ---------------------------------------------------------------------------


class PolicyOptInBody(BaseModel):
    join: bool = False


@router.post("/policy/opt-in")
def policy_opt_in(body: PolicyOptInBody, request: Request):
    user_id = _require_user(request)
    from agent.policy_consent import POLICY_CHANNEL_ID, record_consent

    try:
        if body.join:
            try:
                _bot_client().conversations_invite(channel=POLICY_CHANNEL_ID, users=user_id)
            except Exception as exc:
                # Same as the Slack handler: an invite failure doesn't block
                # consent — the user asked to opt in, not to get invited.
                logger.warning("could not invite user to policy channel: %s", exc)
        record_consent(user_id, joined_policy_channel=body.join)
    except Exception:
        logger.exception("Failed to record policy opt-in for %s", user_id)
        raise HTTPException(500, "Couldn't record opt-in")
    return {"ok": True}


@router.post("/policy/opt-out")
def policy_opt_out(request: Request):
    user_id = _require_user(request)
    from agent.policy_consent import revoke_consent

    revoke_consent(user_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# BYOK endpoints
# ---------------------------------------------------------------------------


class ByokBody(BaseModel):
    name: str
    base_url: str
    api_key: str = ""
    model: str = ""


class ByokPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None


@router.post("/byok")
def byok_add(body: ByokBody, request: Request):
    user_id = _require_user(request)
    from agent.byok_store import add_endpoint

    try:
        ep_id = add_endpoint(user_id, body.name.strip(), body.base_url.strip(), body.api_key, body.model.strip())
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"id": ep_id}


@router.patch("/byok/{ep_id}")
def byok_update(ep_id: str, body: ByokPatch, request: Request):
    user_id = _require_user(request)
    from agent.byok_store import get_endpoint_decrypted, update_endpoint

    ep = get_endpoint_decrypted(user_id, ep_id)
    if not ep:
        raise HTTPException(404, "Endpoint not found")
    try:
        update_endpoint(
            user_id,
            ep_id,
            body.name.strip() if body.name is not None else ep["name"],
            body.base_url.strip() if body.base_url is not None else ep["base_url"],
            body.api_key if body.api_key is not None else "••••••••",
            body.model.strip() if body.model is not None else ep.get("model", ""),
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@router.delete("/byok/{ep_id}")
def byok_delete(ep_id: str, request: Request):
    user_id = _require_user(request)
    from agent.byok_store import delete_endpoint

    delete_endpoint(user_id, ep_id)
    return {"ok": True}


class EndpointSelectBody(BaseModel):
    endpoint_id: str | None = None


@router.post("/byok/text-default")
def byok_text_default(body: EndpointSelectBody, request: Request):
    user_id = _require_user(request)
    from agent.byok_store import set_text_endpoint

    ep_id = None if body.endpoint_id in (None, "", "none") else body.endpoint_id
    try:
        set_text_endpoint(user_id, ep_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


@router.post("/byok/image-default")
def byok_image_default(body: EndpointSelectBody, request: Request):
    user_id = _require_user(request)
    from agent.byok_store import set_image_endpoint

    ep_id = None if body.endpoint_id in (None, "", "none") else body.endpoint_id
    try:
        set_image_endpoint(user_id, ep_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Custom instructions
# ---------------------------------------------------------------------------


class InstructionsBody(BaseModel):
    text: str = ""


@router.put("/instructions")
def set_instructions(body: InstructionsBody, request: Request):
    user_id = _require_user(request)
    from listeners.actions.instructions_actions import set_user_instructions

    set_user_instructions(user_id, body.text)
    return {"ok": True}


# ---------------------------------------------------------------------------
# AI fallback cache
# ---------------------------------------------------------------------------


@router.post("/fallback-cache/clear")
def fallback_cache_clear(request: Request):
    _require_user(request)
    from agent.fallback_cache import clear_cache

    clear_cache()
    return {"ok": True}


@router.post("/providers/test")
def providers_test(request: Request):
    user_id = _require_user(request)
    from agent.admin_alerts import is_admin

    if not is_admin(user_id):
        raise HTTPException(403, "Only the coolton maintainer can run provider tests.")
    from agent.provider_config import build_provider_order
    from agent.provider_probe import probe_all

    order = build_provider_order(user_id)
    if not order:
        return {"results": []}
    results = []
    for provider_name, ok, display, elapsed, detail in probe_all(order):
        results.append({"provider": provider_name, "ok": ok, "display": display, "elapsed": elapsed, "detail": detail})
    return {"results": results}


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


class McpBody(BaseModel):
    name: str
    url: str
    token: str = ""
    transport: str = "auto"
    headers: str = ""


@router.post("/mcp")
def mcp_add(body: McpBody, request: Request):
    user_id = _require_user(request)
    from agent.mcp_server_store import add_server, parse_headers, probe_server

    try:
        headers = parse_headers(body.headers)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    ok, detail, transport = probe_server(body.url.strip(), body.token, body.transport, headers)
    if not ok:
        raise HTTPException(400, f"Couldn't connect to that MCP server, not saved: {detail}")
    try:
        server_id = add_server(user_id, body.name.strip(), body.url.strip(), body.token, transport, headers)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"id": server_id, "detail": detail}


@router.delete("/mcp/{server_id}")
def mcp_delete(server_id: str, request: Request):
    user_id = _require_user(request)
    from agent.mcp_server_store import delete_server

    delete_server(user_id, server_id)
    return {"ok": True}
