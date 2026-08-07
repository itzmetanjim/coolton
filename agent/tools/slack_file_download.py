import os
import re
import requests
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from e2b import Sandbox


SLACK_FILE_URL_PATTERN = re.compile(
    r"https?://[\w.-]+\.slack\.com/files/[\w-]+/([A-Z0-9]+)/"
)


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


def extract_file_id(url: str) -> str | None:
    """Extract Slack file ID from various URL formats.
    
    Supported formats:
    - https://workspace.slack.com/files/USER/FILE_ID/filename
    - https://workspace.enterprise.slack.com/files/USER/FILE_ID/filename
    - Just the file ID (e.g., F0B35316GS1)
    """
    if re.match(r"^F[A-Z0-9]+$", url):
        return url
    match = SLACK_FILE_URL_PATTERN.search(url)
    if match:
        return match.group(1)
    return None


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
        return f"Error: Not a Slack file id: {file_id}. get_slack_file only downloads Slack files; use fetch_url for arbitrary web URLs."

    # Get file info
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
            return f"Slack API error: {err}"

        file_info = info.get("file", {})
        file_url = file_info.get("url_private_download") or file_info.get("url_private")
        default_name = file_info.get("name", file_id)
        mimetype = file_info.get("mimetype", "")
        expected_size = file_info.get("size", 0)

        if not file_url:
            return f"Error: No download URL available for file {file_id}. It may have been deleted, or the token may not have access."

        if not is_slack_host(file_url):
            return f"Error: Refusing to download from a non-Slack host: {file_url}. get_slack_file only downloads Slack-hosted files (it authenticates with the workspace token)."

        # Strip path separators and reject bare "." / ".." so a crafted
        # filename can't escape downloads/ when joined into the sandbox path.
        sanitized = re.sub(r"[^\w.-]+", "_", filename or default_name)
        if sanitized in ("", ".", ".."):
            sanitized = "slack-file"

        # Download the file
        file_resp = requests.get(file_url, headers={"Authorization": f"Bearer {token}"}, timeout=300, stream=True)
        if file_resp.status_code != 200:
            return f"Error: Failed to download file (HTTP {file_resp.status_code})"

        content = file_resp.content

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


def download_file_from_url(
    url: str,
    user_token: str | None = None,
    sandbox: "Sandbox | None" = None,
) -> str:
    """Download a Slack file from a URL (auto-extracts file ID).
    
    Args:
        url: Full Slack file URL
        user_token: Slack user token
        sandbox: Optional E2B sandbox to save file to
        
    Returns:
        Summary of download result
    """
    file_id = extract_file_id(url)
    if not file_id:
        return f"Error: Could not extract file ID from URL: {url}"
    return download_file_by_id(file_id, user_token, sandbox)