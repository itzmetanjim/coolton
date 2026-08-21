"""Slack app creation, token registration, and safe Worker deployment helpers."""
from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path
from typing import Any

STORE = Path(os.environ.get("COOLTON_BOT_STORE", "~/.coolton_bots.json")).expanduser()


def _load() -> dict[str, Any]:
    try:
        return json.loads(STORE.read_text())
    except FileNotFoundError:
        return {}


def _save(data: dict[str, Any]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="coolton-bots-", dir=STORE.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(name, STORE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


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


def create_slack_bot(manifest: dict) -> str:
    """Validate and create a Slack app from a manifest without exposing secrets."""
    if not isinstance(manifest, dict) or not manifest.get("display_information", {}).get("name"):
        return "Error: manifest.display_information.name is required."
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
    store[app_id] = {"app_id": app_id, "credentials": creds}
    _save(store)
    result = {
        "uuid": app_id,
        "app_id": app_id,
        "oauth_authorize_url": created.get("oauth_authorize_url", ""),
    }
    if creds.get("signing_secret"):
        result["signing_secret"] = creds["signing_secret"]
    return json.dumps(result)


def update_slack_bot_manifest(uuid: str, manifest: dict) -> str:
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
    store = _load()
    if uuid not in store:
        return f"Error: unknown bot UUID: {uuid}"
    validated = _api("apps.manifest.validate", {"manifest": manifest, "app_id": uuid})
    if not validated.get("ok"):
        return f"Slack API error: {validated}"
    updated = _api("apps.manifest.update", {"app_id": uuid, "manifest": manifest})
    if not updated.get("ok"):
        return f"Slack API error: {updated}"
    return f"Manifest updated for app {uuid}."


def register_bot_tokens(uuid: str, bot_token: str, app_token: str = "", signing_secret: str = "") -> str:
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
    if uuid not in store:
        return f"Error: unknown bot UUID: {uuid}"
    store[uuid]["bot_token"] = bot_token
    if app_token:
        store[uuid]["app_token"] = app_token
    if signing_secret:
        store[uuid]["signing_secret"] = signing_secret
    _save(store)
    return "Bot tokens registered securely."


def wrangler_bot_deploy(uuid: str, working_dir: str, channel_id: str, thread_ts: str, additional_flags: str = "") -> str:
    """Deploy a Slack bot Worker inside the E2B sandbox.

    Injects stored secrets, runs wrangler deploy, then cleans up.
    """
    from agent.sandbox_helpers import get_or_create_sandbox

    record = _load().get(uuid)
    if not record or not record.get("bot_token"):
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
        cmd_parts = ["cd", working_dir, "&&", "npx", "wrangler@latest", "deploy", "--temporary", "--secrets-file", ".env_slack"]
        if additional_flags:
            cmd_parts += shlex.split(additional_flags)
        cmd = " ".join(cmd_parts)
        result = sandbox.commands.run(cmd)
        output = (result.stdout or "") + (result.stderr or "")
        if result.exit_code:
            return f"Error: wrangler deploy failed (exit {result.exit_code}):\n{output}"
        return output
    except Exception as e:
        return f"Error: {e}"
    finally:
        try:
            sandbox.commands.run(f"rm -f {env_path}")
        except Exception:
            pass
