import os
import re
import requests
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from e2b import Sandbox


SLACK_FILE_URL_PATTERN = re.compile(
    r"https?://[\w.-]+\.slack\.com/files/[\w-]+/([A-Z0-9]+)/"
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
) -> str:
    """Download a Slack file by its file ID.
    
    Downloads to sandbox if provided, otherwise returns file content.
    
    Args:
        file_id: Slack file ID (e.g., F0B35316GS1)
        user_token: Slack user token (defaults to SLACK_USER_TOKEN env)
        sandbox: Optional E2B sandbox to save file to
        
    Returns:
        Summary of download result
    """
    token = user_token or os.environ.get("SLACK_USER_TOKEN")
    if not token:
        return "Error: SLACK_USER_TOKEN not configured"
    
    if not re.match(r"^F[A-Z0-9]+$", file_id):
        return f"Error: Invalid file ID format: {file_id}"
    
    # Get file info
    info_url = "https://slack.com/api/files.info"
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        resp = requests.get(info_url, headers=headers, params={"file": file_id}, timeout=10)
        info = resp.json()
        
        if not info.get("ok"):
            return f"Slack API error: {info.get('error', 'unknown')}"
        
        file_info = info.get("file", {})
        file_url = file_info.get("url_private_download") or file_info.get("url_private")
        filename = file_info.get("name", file_id)
        
        if not file_url:
            return f"Error: No download URL available for file {file_id}"
        
        # Download the file
        file_resp = requests.get(file_url, headers={"Authorization": f"Bearer {token}"}, timeout=300, stream=True)
        if file_resp.status_code != 200:
            return f"Error: Failed to download file (HTTP {file_resp.status_code})"
        
        content = file_resp.content
        
        if sandbox:
            # Save to sandbox
            sandbox.commands.run("mkdir -p ~/attachments")
            sandbox.files.write(f"/home/user/attachments/{filename}", content)
            return f"Downloaded {filename} ({len(content)} bytes) to ~/attachments/"
        else:
            # Return base64 encoded content for other uses
            import base64
            b64 = base64.b64encode(content).decode()
            return f"Downloaded {filename} ({len(content)} bytes). Base64: {b64[:100]}..."
            
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