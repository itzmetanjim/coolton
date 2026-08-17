#!/usr/bin/env python3
"""
Tool: wranglerBotDeploy

Deploy a Slack bot to Cloudflare Workers using Wrangler, securely injecting bot tokens.

Args:
    uuid (str): The Slack app UUID (e.g., "A0123456789").
    working_dir (str): Directory containing the bot code (e.g., "/home/user/sites/slack-bot").
    additional_flags (str, optional): Additional flags for `wrangler deploy` (e.g., "--name my-bot").

Returns:
    dict: {
        "preview_url": str,  // e.g., "https://slack-bot.<subdomain>.workers.dev"
        "claim_url": str,    // e.g., "https://dash.cloudflare.com/...
        "success": bool
    }

Rules:
    - Only bot tokens (xoxb-...) and app tokens (xapp-...) are allowed. User tokens (xoxp-...) are rejected.
    - Tokens are retrieved internally using the UUID and injected into `.env_slack`.
    - `.env_slack` is deleted after deployment.
    - The tool runs `npx wrangler deploy --temporary --secrets-file .env_slack <additional_flags>`.
"""

import os
import subprocess
import tempfile
import json
import sys
from typing import Dict, Any


def get_slack_tokens(uuid: str) -> Dict[str, str]:
    """
    Retrieve Slack tokens for the given UUID.
    This is a placeholder for the actual implementation, which would use the Slack API.
    
    Args:
        uuid (str): The Slack app UUID.

    Returns:
        dict: {"SLACK_BOT_TOKEN": "xoxb-...", "SLACK_APP_TOKEN": "xapp-...", "SLACK_SIGNING_SECRET": "..."}
        
    Raises:
        ValueError: If the UUID is invalid or tokens cannot be retrieved.
    """
    # Placeholder: In a real implementation, this would call the Slack API
    # or a secure internal service to retrieve the tokens.
    if not uuid.startswith("A"):
        raise ValueError("Invalid UUID format. Must start with 'A'.")
    
    return {
        "SLACK_BOT_TOKEN": "xoxb-placeholder-bot-token",
        "SLACK_APP_TOKEN": "xapp-placeholder-app-token",
        "SLACK_SIGNING_SECRET": "placeholder-signing-secret"
    }


def validate_tokens(tokens: Dict[str, str]) -> None:
    """Validate that only bot/app tokens are present (no user tokens)."""
    for key, value in tokens.items():
        if value.startswith("xoxp-"):
            raise ValueError(f"User tokens (xoxp-...) are not allowed. Found in {key}.")


def run_wrangler_deploy(working_dir: str, additional_flags: str = "") -> Dict[str, Any]:
    """Run `npx wrangler deploy --temporary --secrets-file .env_slack` in the working directory."""
    env_slack_path = os.path.join(working_dir, ".env_slack")
    
    try:
        # Run wrangler deploy
        cmd = f"npx wrangler@latest deploy --temporary --secrets-file {env_slack_path} {additional_flags}"
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=working_dir,
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0:
            return {
                "success": False,
                "error": result.stderr
            }
        
        # Parse output for preview_url and claim_url
        output = result.stdout
        preview_url = None
        claim_url = None
        
        for line in output.split("\n"):
            if "Published" in line and "workers.dev" in line:
                preview_url = line.split("https://")[1].split(" ")[0]
                preview_url = f"https://{preview_url}"
            elif "claim" in line.lower() and "cloudflare.com" in line:
                claim_url = line.split("https://")[1].split(" ")[0]
                claim_url = f"https://{claim_url}"
        
        if not preview_url or not claim_url:
            return {
                "success": False,
                "error": "Could not parse preview_url or claim_url from wrangler output."
            }
        
        return {
            "success": True,
            "preview_url": preview_url,
            "claim_url": claim_url
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }
    finally:
        # Clean up .env_slack
        if os.path.exists(env_slack_path):
            os.remove(env_slack_path)


def wrangler_bot_deploy(uuid: str, working_dir: str, additional_flags: str = "") -> Dict[str, Any]:
    """Main function for the wranglerBotDeploy tool."""
    try:
        # Validate working_dir
        if not os.path.isdir(working_dir):
            raise ValueError(f"Working directory does not exist: {working_dir}")
        
        # Get Slack tokens
        tokens = get_slack_tokens(uuid)
        validate_tokens(tokens)
        
        # Write tokens to .env_slack
        env_slack_path = os.path.join(working_dir, ".env_slack")
        with open(env_slack_path, "w") as f:
            for key, value in tokens.items():
                f.write(f"{key}={value}\n")
        
        # Run wrangler deploy
        result = run_wrangler_deploy(working_dir, additional_flags)
        
        if not result["success"]:
            return {
                "success": False,
                "error": result["error"]
            }
        
        return {
            "success": True,
            "preview_url": result["preview_url"],
            "claim_url": result["claim_url"]
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


if __name__ == "__main__":
    # Read JSON input from stdin
    input_data = json.load(sys.stdin)
    
    uuid = input_data.get("uuid")
    working_dir = input_data.get("working_dir")
    additional_flags = input_data.get("additional_flags", "")
    
    result = wrangler_bot_deploy(uuid, working_dir, additional_flags)
    print(json.dumps(result))