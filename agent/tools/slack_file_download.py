import os
import re
import requests
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from e2b import Sandbox


def is_slack_host(raw_url: str) -> bool:
    """True only if the URL is Slack-hosted (defends against a spoofed
    files.info response pointing the bot token at another host)."""
    from urllib.parse import urlparse

    try:
        host = urlparse(raw_url).hostname or ""
    except ValueError:
        return False
    host = host.lower()
    return (
        host == "slack.com"
        or host.endswith(".slack.com")
        or host == "slack-files.com"
        or host.endswith(".slack-files.com")
    )


def download_file_by_id(
    file_id: str,
    user_token: str | None = None,
    sandbox: "Sandbox | None" = None,
    filename: str = "",
) -> str:
    """Download a Slack file by its file ID.

    Downloads to sandbox `~/downloads/` if provided, otherwise returns file content.

    Args:
        file_id: Slack file ID (e.g., F0B35316GS1)
        user_token: Slack user token (defaults to SLACK_USER_TOKEN env)
        sandbox: Optional E2B sandbox to save file to
        filename: Optional name to save the file as (overrides the file's own name).

    Returns:
        Summary of download result
    """
    token = user_token or os.environ.get("SLACK_USER_TOKEN")
    if not token:
        return "Error: SLACK_USER_TOKEN not configured"

    # Accept a Slack file permalink too; pull out the F... id.
    match = re.search(r"\b(F[A-Z0-9]{6,})\b", file_id)
    file_id = match.group(1) if match else file_id
    if not re.match(r"^F[A-Z0-9]+$", file_id):
        return f"Error: Not a Slack file id: {file_id}. get_slack_file only downloads Slack files; use fetch_url for arbitrary web URLs."    # Get file info
    info_url = "https://slack.com/api/files.info"
    headers = {"Authorization": f"Bearer {token}"}

    try:
        resp = requests.get(info_url, headers=headers, params={"file": file_id}, timeout=10)
        info = resp.json()

        if not info.get("ok"):
            err = info.get("error", "unknown")
            if err == "missing_scope":
                return (
                    "Error: the coolton app is missing the `files:read` scope, so files.info "
                    "is not available (needed to resolve the download URL by file id). "
                    "Ask an admin to reinstall the app with files:read, or share the file "
                    "as an attachment in a message so download_attachments_to_sandbox can grab it."
                )
            if err == "file_not_found":
                return (
                    f"Slack API error: file_not_found — no file matches '{file_id}'. Use the "
                    "exact file id (F...) from the message's attachments, not a guessed id."
                )
            return f"Slack API error: {err}"

        file_info = info.get("file", {})
        file_url = file_info.get("url_private_download") or file_info.get("url_private")
        default_name = file_info.get("name", file_id)
        mimetype = file_info.get("mimetype", "")

        if not file_url:
            return f"Error: No download URL available for file {file_id}. It may have been deleted, or the token may not have access."

        if not is_slack_host(file_url):
            return f"Error: Refusing to download from a non-Slack host: {file_url}. get_slack_file only downloads Slack-hosted files (it authenticates with the workspace token)."

        # Strip path separators and reject bare "." / ".." so a crafted
        # filename can't escape downloads/ when joined into the sandbox path.
        sanitized = re.sub(r"[^\w.-]+", "_", filename or default_name)
        sanitized = os.path.basename(sanitized)
        if sanitized in ("", ".", "..") or os.path.isabs(sanitized):
            sanitized = "slack-file"

        # Download the file
        file_resp = requests.get(file_url, headers={"Authorization": f"Bearer {token}"}, timeout=(10, 60), stream=True)
        if file_resp.status_code != 200:
            return f"Error: Failed to download file (HTTP {file_resp.status_code})"

        # Enforce size limit (100 MB)
        MAX_SIZE = 100 * 1024 * 1024
        content_length = file_resp.headers.get("Content-Length") if hasattr(file_resp, "headers") else None
        try:
            if content_length and int(content_length) > MAX_SIZE:
                return f"Error: File exceeds maximum size of {MAX_SIZE // (1024*1024)} MB"
        except (TypeError, ValueError):
            pass
        chunks = bytearray()
        try:
            for chunk in file_resp.iter_content(chunk_size=8192):
                chunks.extend(chunk)
                if len(chunks) > MAX_SIZE:
                    return f"Error: File exceeds maximum size of {MAX_SIZE // (1024*1024)} MB"
            content = bytes(chunks)
        except TypeError:
            # Keep compatibility with simple response doubles and older adapters.
            content = file_resp.content
            if len(content) > MAX_SIZE:
                return f"Error: File exceeds maximum size of {MAX_SIZE // (1024*1024)} MB"

        if sandbox:
            # Save to sandbox downloads/ (mirrors gorkie)
            sandbox.commands.run("mkdir -p ~/downloads")
            sandbox.files.write(f"/home/user/downloads/{sanitized}", content)
            size_str = f"{len(content) / 1024:.0f} KB" if len(content) < 1024 * 1024 else f"{len(content) / 1024 / 1024:.1f} MB"
            return f"Downloaded {sanitized} ({size_str}, {mimetype}) to ~/downloads/{sanitized} in the sandbox."
        else:
            # Return base64 encoded content for other uses
            import base64
            b64 = base64.b64encode(content).decode()
            return f"Downloaded {default_name} ({len(content)} bytes). Base64: {b64[:100]}..."

    except requests.Timeout:
        return "Error: Download timed out"
    except Exception as e:
        return f"Error downloading file: {str(e)}"