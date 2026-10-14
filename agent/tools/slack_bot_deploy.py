"""Slack app creation, token registration, and safe Worker deployment helpers."""
from __future__ import annotations

import json
import os
import shlex
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from agent.json_file import load_json, save_json

STORE = Path(os.environ.get("COOLTON_BOT_STORE", "~/.coolton_bots.json")).expanduser()

# Public base URL of the coolton process itself (web/server.py, started by
# app.py's _start_web_ui) — NOT the Worker being deployed, which doesn't exist
# yet at manifest-create time. web/bot_oauth.py's callback route lives here.
PUBLIC_BASE_URL = os.environ.get("COOLTON_PUBLIC_URL", "https://coolton.tanjim.org")
OAUTH_CALLBACK_PATH = "/bot-oauth/callback"
_STATE_MAX_AGE_SECONDS = 24 * 3600
_DEPLOY_TIMEOUT_SECONDS = 600


def oauth_callback_url() -> str:
    return PUBLIC_BASE_URL.rstrip("/") + OAUTH_CALLBACK_PATH


def sign_install_state(app_id: str) -> str:
    """Sign a short-lived `state` token binding an OAuth install attempt to the
    app it was created for. web/bot_oauth.py verifies this on the way back
    from Slack so a forged/foreign `state` can never register a token onto an
    app it doesn't belong to."""
    from agent.signing import sign

    return sign("bot-install", {"app_id": app_id, "issued_at": time.time()})


def verify_install_state(state: str) -> str | None:
    """Inverse of sign_install_state: the app_id if `state` is validly signed
    and not expired, else None."""
    from agent.signing import verify

    payload = verify("bot-install", state)
    if not payload or not isinstance(payload.get("app_id"), str):
        return None
    try:
        if time.time() - float(payload.get("issued_at")) > _STATE_MAX_AGE_SECONDS:
            return None
    except (TypeError, ValueError):
        return None
    return payload["app_id"]


def _load() -> dict[str, Any]:
    return load_json(STORE)


def _save(data: dict[str, Any]) -> None:
    save_json(STORE, data, private=True, indent=None)  # bot tokens and signing secrets


def _api(method: str, data: dict[str, Any]) -> dict[str, Any]:
    import requests
    token = os.environ.get("SLACK_CONFIG_TOKEN")
    if not token:
        try:
            from agent.token_rotation import get_access_token
            token = get_access_token()
        except Exception:
            pass
    if not token:
        return {"ok": False, "error": "SLACK_CONFIG_TOKEN not configured and no xoxe token available."}
    try:
        response = requests.post(
            f"https://slack.com/api/{method}",
            headers={"Authorization": f"Bearer {token}"},
            data={k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in data.items()},
            timeout=30,
        )
        return response.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _owned_record(store: dict[str, Any], uuid: str, requester_id: str) -> tuple[dict[str, Any] | None, str | None]:
    """(record, None) if `requester_id` may manage bot `uuid`, else (None, error).

    Each bot belongs to whoever created it (the record's `owner_id`); only
    they — or the maintainer — can check it, register tokens on it, change its
    manifest, or deploy it. Without this, anyone who learned an app id could
    rewrite someone else's app or deploy it with its real bot token and signing
    secret written into their own sandbox."""
    from agent.admin_alerts import ADMIN_USER_ID

    record = store.get(uuid)
    if not record:
        return None, f"Error: unknown bot UUID: {uuid}"
    from agent.redact import add_runtime_secret

    for key in ("bot_token", "app_token", "signing_secret"):
        add_runtime_secret(f"DEPLOYED_BOT_{key.upper()}", str(record.get(key) or ""))
    add_runtime_secret("DEPLOYED_BOT_SIGNING_SECRET", str((record.get("credentials") or {}).get("signing_secret") or ""))
    if requester_id != ADMIN_USER_ID and (not requester_id or record.get("owner_id") != requester_id):
        return None, f"Error: bot {uuid} was created by someone else; only its creator can manage it."
    return record, None


def create_slack_bot(manifest: dict, owner_id: str) -> str:
    """Validate and create a Slack app from a manifest without exposing secrets.

    Bakes coolton's own OAuth callback into the app's redirect_urls, so the
    returned oauth_authorize_url carries a redirect_uri + signed state and a
    human never has to dig the bot token out of the Slack UI and hand it back
    — web/bot_oauth.py captures and registers it automatically. Poll
    check_bot_install_status(app_id) to know when that's happened.
    """
    if not isinstance(manifest, dict) or not manifest.get("display_information", {}).get("name"):
        return "Error: manifest.display_information.name is required."

    manifest = dict(manifest)  # don't mutate the caller's dict
    oauth_config = dict(manifest.get("oauth_config") or {})
    callback_url = oauth_callback_url()
    redirect_urls = list(oauth_config.get("redirect_urls") or [])
    if callback_url not in redirect_urls:
        redirect_urls.append(callback_url)
    oauth_config["redirect_urls"] = redirect_urls
    manifest["oauth_config"] = oauth_config

    validated = _api("apps.manifest.validate", {"manifest": manifest})
    if not validated.get("ok"):
        return f"Slack API error: {validated}"
    # apps.manifest.create does not accept app_id (that's only for
    # apps.manifest.update, once the app already exists).
    created = _api("apps.manifest.create", {"manifest": manifest})
    if not created.get("ok"):
        return f"Slack API error: {created}"
    app_id = created.get("app_id") or created.get("app", {}).get("id")
    if not app_id:
        return "Slack API error: apps.manifest.create returned no app_id."
    creds = created.get("credentials", {})
    store = _load()
    store[app_id] = {"app_id": app_id, "owner_id": owner_id, "credentials": creds}
    _save(store)
    # signing_secret is intentionally NOT included here: it's already persisted in
    # `store` above, and wrangler_bot_deploy already falls back to reading it from
    # there (record["credentials"]["signing_secret"]) if register_bot_tokens never
    # sets it directly. Returning it here would put it in the model's context —
    # from where it could end up in a Slack message or a conversation trace — for
    # no functional benefit.
    scopes = oauth_config.get("scopes", {})
    authorize_params = {
        "client_id": creds.get("client_id", ""),
        "scope": " ".join(scopes.get("bot", [])),
        "redirect_uri": callback_url,
        "state": sign_install_state(app_id),
    }
    if scopes.get("user"):
        authorize_params["user_scope"] = " ".join(scopes["user"])
    result = {
        "uuid": app_id,
        "app_id": app_id,
        "oauth_authorize_url": "https://slack.com/oauth/v2/authorize?" + urlencode(authorize_params),
        "auto_install": True,
    }
    return json.dumps(result)


def get_bot_record(uuid: str) -> dict[str, Any] | None:
    """Read-only lookup for a stored bot's record (credentials, tokens once
    registered). Used by web/bot_oauth.py during the auto-install callback."""
    return _load().get(uuid)


def check_bot_install_status(uuid: str, requester_id: str) -> str:
    """Report whether a human has completed the OAuth install for this app yet.

    Poll this (rather than asking the user to paste a token back) after
    handing them the oauth_authorize_url from create_slack_bot — the callback
    registers the bot token automatically the moment they finish installing.
    """
    record, error = _owned_record(_load(), uuid, requester_id)
    if error:
        return error
    if record.get("bot_token"):
        return "installed: the app has been installed and its bot token is registered. Ready to deploy."
    return "not_installed: still waiting for a human to visit the oauth_authorize_url and complete the install."


def update_slack_bot_manifest(uuid: str, manifest: dict, requester_id: str) -> str:
    """Update an already-created Slack app's manifest (apps.manifest.update).

    Use this once the Worker is actually deployed and its real URL is known, to point
    slash_commands[].url / settings.event_subscriptions.request_url at it — Slack
    verifies those request URLs live (a challenge/response handshake for event
    subscriptions), so they can't be set correctly until the Worker is already up.
    The manifest passed here REPLACES the app's entire configuration, so include every
    field (scopes, bot_user, etc.), not just the URL you're changing.
    """
    if not isinstance(manifest, dict) or not manifest.get("display_information", {}).get("name"):
        return "Error: manifest.display_information.name is required."
    _record, error = _owned_record(_load(), uuid, requester_id)
    if error:
        return error
    validated = _api("apps.manifest.validate", {"manifest": manifest, "app_id": uuid})
    if not validated.get("ok"):
        return f"Slack API error: {validated}"
    updated = _api("apps.manifest.update", {"app_id": uuid, "manifest": manifest})
    if not updated.get("ok"):
        return f"Slack API error: {updated}"
    return f"Manifest updated for app {uuid}."


def register_bot_tokens(
    uuid: str, bot_token: str, app_token: str = "", signing_secret: str = "", *, requester_id: str,
) -> str:
    """Store bot/app credentials for a created app; reject user tokens.

    app_token (xapp-) is only meaningful for Socket Mode apps — it's generated
    manually in the app's Basic Information page, separate from the OAuth install
    flow, and most HTTP-mode Workers (the pattern this tool targets) never have
    one. Only bot_token is required; app_token is validated/stored if provided.
    """
    if not uuid or not bot_token.startswith("xoxb-"):
        return "Error: only xoxb- bot tokens are accepted for bot_token."
    if app_token and not app_token.startswith("xapp-"):
        return "Error: app_token must start with xapp- (omit it entirely if this bot doesn't use Socket Mode)."
    if signing_secret and signing_secret.startswith("xoxp-"):
        return "Error: invalid signing secret."
    store = _load()
    _record, error = _owned_record(store, uuid, requester_id)
    if error:
        return error
    return _store_tokens(store, uuid, bot_token, app_token, signing_secret)


def store_installed_bot_token(uuid: str, bot_token: str) -> str:
    """Record the bot token captured by the OAuth install callback
    (web/bot_oauth.py). No requester check: the callback is already bound to
    this app by its signed `state`, and whoever completes Slack's install
    dialog isn't necessarily the bot's creator."""
    if not bot_token.startswith("xoxb-"):
        return "Error: only xoxb- bot tokens are accepted for bot_token."
    store = _load()
    if uuid not in store:
        return f"Error: unknown bot UUID: {uuid}"
    return _store_tokens(store, uuid, bot_token)


def _store_tokens(store: dict[str, Any], uuid: str, bot_token: str, app_token: str = "", signing_secret: str = "") -> str:
    store[uuid]["bot_token"] = bot_token
    if app_token:
        store[uuid]["app_token"] = app_token
    if signing_secret:
        store[uuid]["signing_secret"] = signing_secret
    _save(store)
    return "Bot tokens registered securely."


def wrangler_bot_deploy(
    uuid: str, working_dir: str, channel_id: str, thread_ts: str, additional_flags: str = "", *, requester_id: str,
) -> str:
    """Deploy a Slack bot Worker inside the E2B sandbox.

    Injects stored secrets, runs wrangler deploy, then cleans up.
    """
    from agent.sandbox_helpers import get_or_create_sandbox, run_command

    record, error = _owned_record(_load(), uuid, requester_id)
    if error:
        return error
    if not record.get("bot_token"):
        return "Error: a bot token is not registered for this UUID. Call register_bot_tokens first."

    try:
        sandbox, _ = get_or_create_sandbox(channel_id, thread_ts)
    except Exception as e:
        return f"Error connecting to sandbox: {e}"

    env_lines = [
        f"SLACK_BOT_TOKEN={record['bot_token']}",
        f"SLACK_SIGNING_SECRET={record.get('signing_secret', record.get('credentials', {}).get('signing_secret', ''))}",
    ]
    # app_token (xapp-) is Socket-Mode-only; only write it if this bot actually has one.
    if record.get("app_token"):
        env_lines.append(f"SLACK_APP_TOKEN={record['app_token']}")
    env_content = "\n".join(env_lines) + "\n"
    env_path = f"{working_dir.rstrip('/')}/.env_slack"

    try:
        sandbox.files.write(env_path, env_content)
        deploy = ["npx", "wrangler@latest", "deploy", "--temporary", "--secrets-file", ".env_slack"]
        if additional_flags:
            deploy += shlex.split(additional_flags)
        cmd = f"cd {shlex.quote(working_dir)} && {shlex.join(deploy)}"
        # An npx download plus a deploy routinely takes longer than the 60s default.
        result = run_command(sandbox, cmd, timeout=_DEPLOY_TIMEOUT_SECONDS)
        output = (result.stdout or "") + (result.stderr or "")
        if result.exit_code:
            return f"Error: wrangler deploy failed (exit {result.exit_code}):\n{output}"
        return output
    except Exception as e:
        return f"Error: {e}"
    finally:
        try:
            sandbox.commands.run(f"rm -f {shlex.quote(env_path)}")
        except Exception:
            pass
