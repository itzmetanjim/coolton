"""Add a custom Slack emoji (upload a new image, or alias an existing name).

Slack's Web API has no public method for this — admin.emoji.add exists but
is undocumented and workspace-admin-only. Routes through the same open Hack
Club community proxy gorkie uses instead of reimplementing that workaround.
Optional: no-ops with a clear error if EMOJI_PROXY_TOKEN isn't set.
"""

import os
import re

import requests

EMOJI_PROXY_URL = "https://hackclub-slack-emoji-proxy.vercel.app/api/emoji"
_NAME_RE = re.compile(r"^[a-z0-9_+-]+$")
_REQUEST_TIMEOUT_SECONDS = 30


def upload_emoji(channel_id: str, thread_ts: str, name: str, path: str = "", alias_for: str = "") -> str:
    token = os.environ.get("EMOJI_PROXY_TOKEN")
    if not token:
        return "Error: emoji upload is not configured. Set EMOJI_PROXY_TOKEN to enable it."

    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        return (
            "Error: emoji names are lowercase letters, numbers, dashes, and underscores "
            "only, no spaces or colons."
        )
    if bool(path) == bool(alias_for):
        return "Error: pass exactly one of path (to upload a new image) or alias_for (an existing emoji name)."

    headers = {"Authorization": f"Bearer {token}"}
    try:
        if alias_for:
            response = requests.post(
                f"{EMOJI_PROXY_URL}/alias",
                headers={**headers, "Content-Type": "application/json"},
                json={"name": name, "alias_for": alias_for},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        else:
            from agent.sandbox_helpers import get_or_create_sandbox
            sandbox, _ = get_or_create_sandbox(channel_id, thread_ts)
            content = bytes(sandbox.files.read(path, format="bytes"))
            filename = path.rsplit("/", 1)[-1] or name
            response = requests.post(
                f"{EMOJI_PROXY_URL}/upload",
                headers=headers,
                files={"file": (filename, content)},
                data={"name": name},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
    except Exception as e:
        return f"Error {'aliasing' if alias_for else 'uploading'} emoji: {e}"

    if not response.ok:
        kind = "alias" if alias_for else "upload"
        return f"Emoji {kind} failed ({response.status_code}): {response.text[:300]}"
    return f"Added :{name}:."
