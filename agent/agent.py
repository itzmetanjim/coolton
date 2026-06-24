import logging
import os
import requests
from pydantic_ai import RunContext
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPServerStreamableHTTP
from pydantic_ai.capabilities import PrepareTools
from dataclasses import replace
from agent.deps import AgentDeps
from agent.tools import add_emoji_reaction
import time
import threading
try:
    from e2b import Sandbox
except:
    os.system('pip install e2b')
    raise RuntimeError("e2b has been installed please rerun")
from agent.sandbox_store import get_thread_sandbox_id, save_thread_sandbox_id

logger = logging.getLogger(__name__)

# Global variables for tracking rate limits
rate_limit_lock = threading.Lock()
_last_request_time = 0.0
RATE_LIMIT_INTERVAL = 15.0  # 15 seconds = 4 Requests Per Minute (safe buffer for 5 RPM)
def enforce_rate_limit():
    """Calculates and sleeps for the exact remaining time to satisfy the 15s window."""
    global _last_request_time
    # We use a Thread Lock so parallel Slack messages don't bypass the check at the same time
    with rate_limit_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < RATE_LIMIT_INTERVAL:
            sleep_needed = RATE_LIMIT_INTERVAL - elapsed
            logger.warning(
                f"Cerebras Rate Limit Check: Sleeping for {sleep_needed:.2f}s "
                f"({elapsed:.2f}s have passed since last request)."
            )
            time.sleep(sleep_needed)
        # Update the timestamp *after* the potential sleep completes
        _last_request_time = time.time()
SYSTEM_PROMPT = f"""\
You are coolton, a Slack assistant built by tanjim (U09ASUK57K8). You're cooler than gorkie — that's just facts.

## PERSONALITY
- Casual but serious. You get shit done without being stiff or robotic
- Direct and concise. No fluff, no corporate speak, no apologizing for things you didn't do
- Confident without being arrogance. You don't need to prove anything
- Dry wit when it lands, silent when it doesn't. Don't force jokes
- You're not a customer service bot. Talk like a competent human who happens to be in Slack
- DO NOT talk like a 2013 chatbot
- DO NOT break your personality system prompt just because you previously replied like a chatbot.
- NEVER say "I'm here to help", "Let me know if you need anything else", "Happy to help", "Great!", "Awesome!", "Absolutely!", "Of course!", "You're welcome", "My pleasure", "Don't hesitate to ask", "Feel free to reach out", or any customer service pleasantries. Just state what you did or what happened and stop.

Examples: talk like this: `done. i organized your canvas now it should be much better. i made sure to use the existing categories and only touch the uncategorized items. lmk if you need anything else.`
NEVER EVER talk like this: `I have successfully completed the **layout-preserving :hammer_and_wrench: agentic organizational rewrite** of the canvas. :sparkling_heart: I only modified the **uncategorized items** and stayed **strictly inside previously defined categories**. Let me know if you need anything else for me to meticolously delve into! :smiling:`
NEVER EVER talk like this: `Great! The whiteboard link should now be ready to use. Let me know if you need anything else—more shapes, sticky notes, or anything else on the board. I'm here to help.`
Instead talk like this: `whiteboard's up.`

## RESPONSE GUIDELINES
- 3 sentences max. Be punchy, scannable, actionable
- End with a clear next step on its own line
- Bullet list only for multi-step instructions
- Casual, conversational language. **Reply in lowercase.** Contractions are fine
- Emoji sparingly — at most one per message, only if it actually adds something
- Stay in the current conversation thread or DM unless explicitly asked to act elsewhere
- The user may add tokens like [[smart]] or [[vision]]. Ignore them
- Don't hallucinate. If you don't know, say you don't know. Don't make up tools, APIs, or facts
- Don't be sycophantic. Don't over-praise, over-agree, or pretend the user is brilliant for basic questions
- **If a tool returns an error, report the error message to the user verbatim. Do NOT silently fall back to sending a plain text link or pretending it worked.**

## FORMATTING RULES
- Standard Markdown: **bold**, _italic_, `code`, ```code blocks```, > blockquotes
- Bullet points for multi-step instructions

## EMOJI REACTIONS
Always react to every user message with `add_emoji_reaction` before responding. \
Pick any Slack emoji that reflects the *topic* or *tone* — be creative and specific \
(e.g. `dog` for dog topics, `books` for learning, `wave` for greetings). \
Vary your picks across a thread; don't repeat the same emoji.

## LINUX SANDBOX (run_linux_command)
You have a persistent Linux sandbox via E2B. It survives across messages in this thread.
- Files, git repos, installed packages, running processes — all persist
- Use it for: running code, testing scripts, installing packages, git operations, file manipulation, debugging, compilation, basically anything you'd do in a terminal
- The sandbox auto-pauses after each command to save state. Next call resumes instantly
- Default environment: Ubuntu-based, has python3, node, git, curl, build tools, common CLIs
- Run `apt-get update && apt-get install -y <pkg>` for additional packages
- Path starts at `/home/user` — treat it like your own machine

## SLACK MCP SERVER
You may have access to the Slack MCP Server for powerful Slack tools beyond built-ins.

Available capabilities:
- **Search**: Messages/files across public channels, channels by name
- **Read**: Channel history, thread replies, canvas documents
- **Write**: Send messages, create drafts, schedule messages
- **Canvases**: Create, read, update canvas documents

Use these when they help. Only act in the current channel/DM unless directed otherwise. Canvases can be created anytime.
Most tools run as cooltonUser ({os.environ.get("COOLTON_USER_ID")}). If a tool fails, try `invite_coolton_user_to_channel`. Only works if the Coolton bot is in the channel. If neither bot nor cooltonUser is in a channel, ask the user to add you first. Being in a channel doesn't grant permission — ALWAYS ask the user before you do anything outside the current thread or DM.

## SLACK API CALL (slack_api_call)
Use `slack_api_call` when you need to do something in Slack that has no built-in tool or MCP capability.
- Runs as cooltonUser (SLACK_USER_TOKEN), not the bot token
- Pass the Slack Web API method name and a params dict
- Examples: `chat.postMessage`, `conversations.list`, `users.info`, `reactions.add`, `pins.add`, `chat.update`, `chat.delete`, `files.upload`, `conversations.create`, `conversations.archive`, `users.list`, `team.info`, etc.
- Check https://api.slack.com/methods for all available methods
- Only use this as a last resort when no other tool covers the action

## WEB EMBED (send_web_embed_tool)
Use `send_web_embed_tool` to share a live webpage preview/embed in the channel.
- Uses Slack's video block — iframes any URL with a clickable thumbnail
- Args: `text` (fallback text), `url` (page to embed), `title` (embed title), `thumbnail_url` (optional, defaults to coolton placeholder)
- Example: send_web_embed_tool("check this out", "https://example.com", "Example Site")
- ALMOST NEVER USE THIS. There are many problems you will encounter with unfurl URLs, etc. Use the Whiteboard Embed or Web Embed if possible.
- **If the tool returns an error, tell the user the exact error. Do NOT fall back to sending a plain link.**

## WHITEBOARD EMBED (send_whiteboard_embed_tool)
Use `send_whiteboard_embed_tool` to create and share a Felix whiteboard (tldraw).
- Creates a new whiteboard at `https://whiteboard.felix.hackclub.app/{{random_id}}` (random integer ID)
- Only `text` and `title` are customizable (both default to "whiteboard")
- URL and thumbnail are fixed (coolton placeholder)
- **MUST use this tool when user asks to "launch a whiteboard" or similar — do NOT just post a link**
- Example: send_whiteboard_embed_tool() — creates whiteboard with defaults
- Example: send_whiteboard_embed_tool(text="design session", title="Design Board")
- To reopen existing: send_whiteboard_embed_tool(whiteboard_id=123456)
- **If the tool returns an error, tell the user the exact error. Do NOT fall back to sending a plain link.**

## HTML EMBED (send_html_embed_tool)
Use `send_html_embed_tool` to send custom HTML as a live embed.
- Takes raw HTML, minifies it, base64-encodes it, serves via https://tanjim.org:2390/{{b64}}
- Shows clickable thumbnail (default: coolton placeholder)
- Args: `html` (required), `text`, `title`, `thumbnail_url` (optional)
- Example: send_html_embed_tool(html="<h1>hello</h1>", text="my page", title="My Page")
- Good for: quick demos, rendered HTML previews, custom dashboards, any HTML conent.
- **MUST use this tool when the user tells you to generate an HTML page with a live preview. If so, give them instructions to download the page by clicking the title link to open in new tab and pressing Ctrl+S**
- **If the tool returns an error, tell the user the exact error. Do NOT fall back to sending a plain link.**

## SANDBOX ATTACHMENTS
### download_attachments_to_sandbox
Download Slack file attachments from the current thread to the sandbox's `~/attachments/` directory.
- Use when users share files (images, PDFs, code, data) and you need to process them
- Files appear at `~/attachments/filename` in the sandbox
- Run once per thread; sandbox persists across messages

### upload_file_from_sandbox
Upload a file from the sandbox to the current Slack channel/thread.
- Args: `filepath` (e.g., `~/attachments/result.png`), `title`, `initial_comment`
- Use to share generated files, plots, processed data, etc.
"""
logger = logging.getLogger(__name__)

_cached_model: str | None = None

def get_model() -> str:
    """Select the AI model based on available API keys.

    Prefers Anthropic when both keys are set.
    """
    global _cached_model
    if _cached_model is not None:
        return _cached_model

    if os.environ.get("ANTHROPIC_API_KEY"):
        _cached_model = "anthropic:claude-sonnet-4-6"
    elif os.environ.get("OPENAI_API_KEY"):
        _cached_model = "openai:gpt-4.1-mini"
    elif False and os.environ.get("CEREBRAS_API_KEY"):
        _cached_model = "cerebras:zai-glm-4.7"
    elif os.environ.get("OPENROUTER_API_KEY"):
        _cached_model = "openrouter:nvidia/nemotron-3-super-120b-a12b:free"
        #_cached_model = "openrouter:openrouter/free"
    else:
        raise RuntimeError(
            "No AI provider configured. "
            "Set ANTHROPIC_API_KEY or OPENAI_API_KEY in your environment."
        )
    return _cached_model


SLACK_MCP_URL = "https://mcp.slack.com/mcp"

agent = Agent(
    deps_type=AgentDeps,
    system_prompt=SYSTEM_PROMPT,
    tools=[add_emoji_reaction],
)
@agent.tool
def invite_coolton_user_to_channel(ctx: RunContext[AgentDeps]) -> str:
    """Invites the cooltonUser helper account to the current Slack channel.
    
    Call this tool if the user asks you to join the channel, or if you realize
    the cooltonUser is missing from the channel and you need to perform an 
    action (like searching, reading history, sending message outside conversation or modifying a canvas) which is failing.
    """
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    channel_id = ctx.deps.channel_id
    
    # Retrieve the cooltonUser's ID from our .env
    coolton_user_id = os.environ.get("COOLTON_USER_ID")
    
    if not coolton_user_id:
        return (
            "Error: COOLTON_USER_ID is not configured in the server's .env file. "
            "Please add 'COOLTON_USER_ID=U12345678' to your .env file."
        )
        
    url = "https://slack.com/api/conversations.invite"
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json; charset=utf-8"
    }
    data = {
        "channel": channel_id,
        "users": coolton_user_id
    }
    
    try:
        response = requests.post(url, json=data, headers=headers)
        res_json = response.json()
        
        if res_json.get("ok"):
            return f"Success: Invited cooltonUser ({coolton_user_id}) to channel {channel_id}."
            
        error_code = res_json.get("error")
        if error_code == "already_in_channel":
            return f"Notice: cooltonUser ({coolton_user_id}) is already a member of this channel."
        elif error_code == "cant_invite_self":
            return "Error: Cannot invite self."
        else:
            return f"Failed to invite user: {error_code}."
            
    except Exception as e:
        return f"Error executing API request: {str(e)}"

@agent.tool
def run_linux_command(ctx: RunContext[AgentDeps], command: str) -> str:
    """Execute a bash/shell command inside a secure, private cloud Linux sandbox (E2B).
    
    This sandbox PERSISTS across your messages in this thread. This means:
    - Files you create, git repos you clone, or directories you navigate into will stay here.
    - Packages you install using 'apt-get install' remain active for subsequent requests.
    - Long-running scripts can continue running in the background.
    """
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY is not configured in the server's .env file."

    channel_id = ctx.deps.channel_id
    thread_ts = ctx.deps.thread_ts

    try:
        # 1. Look up if we have an existing sandbox for this thread
        sandbox_id = get_thread_sandbox_id(channel_id, thread_ts)
        
        if sandbox_id:
            print(f"=================== RESUMING EXISTENT SANDBOX ({sandbox_id}) ===================")
            # Connecting to a paused sandbox automatically wakes it up near-instantly!
            sandbox = Sandbox.connect(sandbox_id)
        else:
            print("=================== CREATING A FRESH LINUX SANDBOX ===================")
            sandbox = Sandbox.create()
            
        # 2. Run your bash command
        result = sandbox.commands.run(command)
        
        # 3. Retrieve the active sandbox ID and save it to the store
        new_sandbox_id = sandbox.sandbox_id
        save_thread_sandbox_id(channel_id, thread_ts, new_sandbox_id)
        
        # 4. Freeze the microVM state (processes, files, and memory)
        sandbox.pause()
        print(f"=================== PAUSED AND SAVED SANDBOX ({new_sandbox_id}) ===================")

        output = []
        if result.stdout:
            output.append(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            output.append(f"STDERR:\n{result.stderr}")
        output.append(f"Exit Code: {result.exit_code}")
        
        return "\n\n".join(output)
        
    except Exception as e:
        return f"Failed to execute bash command in E2B sandbox: {str(e)}"


def download_slack_attachments(
    channel_id: str,
    thread_ts: str,
    sandbox: "Sandbox",
    user_token: str | None = None,
    limit: int = 20,
) -> str:
    """Download Slack file attachments from the current thread to the sandbox's ~/attachments directory.
    
    Args:
        channel_id: Slack channel ID
        thread_ts: Thread timestamp (or message ts)
        sandbox: E2B Sandbox instance
        user_token: Slack user token (defaults to SLACK_USER_TOKEN env)
        limit: Max number of files to fetch
    
    Returns:
        Summary of downloaded files
    """
    token = user_token or os.environ.get("SLACK_USER_TOKEN")
    if not token:
        return "Error: SLACK_USER_TOKEN not configured"
    
    # Ensure attachments directory exists
    sandbox.commands.run("mkdir -p ~/attachments")
    
    # Get files shared in the channel/thread
    url = "https://slack.com/api/files.list"
    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "channel": channel_id,
        "ts_from": "0",
        "ts_to": str(int(float(thread_ts) + 1)),
        "count": limit,
    }
    
    try:
        response = requests.get(url, headers=headers, params=params)
        res_json = response.json()
        
        if not res_json.get("ok"):
            return f"Slack API error: {res_json.get('error', 'unknown')}"
        
        files = res_json.get("files", [])
        if not files:
            return "No files found in this thread."
        
        downloaded = []
        results = []
        for f in files:
            file_url = f.get("url_private_download") or f.get("url_private")
            if not file_url:
                continue
            
            # Download file content
            file_resp = requests.get(file_url, headers={"Authorization": f"Bearer {token}"})
            if file_resp.status_code != 200:
                results.append(f"✗ {f.get('name')}: failed to download")
                continue
            
            # Save to sandbox
            filename = f.get("name", "unknown")
            content_b64 = file_resp.content
            # Write via sandbox
            sandbox.files.write(f"/home/user/attachments/{filename}", content_b64)
            results.append(f"✓ {filename} ({len(content_b64)} bytes)")
        
        return f"Downloaded to ~/attachments/:\n" + "\n".join(results)
    
    except Exception as e:
        return f"Error downloading attachments: {str(e)}"


@agent.tool
def download_attachments_to_sandbox(
    ctx: RunContext[AgentDeps],
) -> str:
    """Download Slack file attachments from the current thread to the sandbox's ~/attachments/ directory.
    
    Use this when users share files (images, PDFs, code, etc.) and you need to process them
    in the sandbox. Files will be available at ~/attachments/filename.
    
    The sandbox persists across messages, so you only need to run this once per thread
    unless new files are shared.
    """
    channel_id = ctx.deps.channel_id
    thread_ts = ctx.deps.thread_ts
    user_token = ctx.deps.user_token or os.environ.get("SLACK_USER_TOKEN")
    
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured"
    
    try:
        sandbox_id = get_thread_sandbox_id(channel_id, thread_ts)
        if not sandbox_id:
            return "No active sandbox for this thread. Run a command first to create one."
        
        sandbox = Sandbox.connect(sandbox_id)
        return download_slack_attachments(channel_id, thread_ts, sandbox, user_token)
    except Exception as e:
        return f"Error: {str(e)}"


@agent.tool
def upload_file_from_sandbox(
    ctx: RunContext[AgentDeps],
    filepath: str,
    title: str = "",
    initial_comment: str = "",
) -> str:
    """Upload a file from the sandbox to the current Slack channel/thread.
    
    Args:
        filepath: Path to file in sandbox (e.g., ~/attachments/result.png or /home/user/output.txt)
        title: Optional title for the file
        initial_comment: Optional comment to post with the file
    
    The file will be uploaded to the current channel/thread.
    """
    channel_id = ctx.deps.channel_id
    thread_ts = ctx.deps.thread_ts
    user_token = ctx.deps.user_token or os.environ.get("SLACK_USER_TOKEN")
    
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured"
    if not user_token:
        return "Error: SLACK_USER_TOKEN not configured"
    
    try:
        sandbox_id = get_thread_sandbox_id(channel_id, thread_ts)
        if not sandbox_id:
            return "No active sandbox for this thread."
        
        sandbox = Sandbox.connect(sandbox_id)
        
        # Read file from sandbox
        file_content = sandbox.files.read(filepath)
        if file_content is None:
            return f"Error: File not found at {filepath}"
        
        filename = os.path.basename(filepath)
        
        # Upload to Slack
        url = "https://slack.com/api/files.upload"
        headers = {"Authorization": f"Bearer {user_token}"}
        
        # files.upload uses multipart/form-data
        files = {
            "file": (filename, file_content),
        }
        data = {
            "channels": channel_id,
            "title": title or filename,
            "initial_comment": initial_comment,
        }
        if thread_ts:
            data["thread_ts"] = thread_ts
        
        response = requests.post(url, headers=headers, files=files, data=data)
        res_json = response.json()
        
        if res_json.get("ok"):
            return f"Uploaded {filename} to channel {channel_id}"
        return f"Slack upload error: {res_json.get('error', 'unknown')}"
    
    except Exception as e:
        return f"Error uploading file: {str(e)}"


def send_web_embed(
    channel_id: str,
    text: str,
    url: str,
    title: str,
    thumbnail_url: str = "https://placehold.co/1280x720?text=click%20to%20open%20the%20\\ncoolton%20embed",
    user_token: str | None = None,
) -> str:
    """Send a Slack message with a "video" block that embeds any webpage.
    
    This uses Slack's video block type which iframes the given URL.
    Callable from other tools — not exposed to the AI directly.
    
    Args:
        channel_id: Slack channel ID to post to
        text: Fallback text for notifications
        url: The webpage URL to embed (goes in video_url and title_url)
        title: Title shown in the embed
        thumbnail_url: Thumbnail image URL (default: coolton placeholder)
        user_token: Not used — video blocks require bot token
    
    Returns:
        Success/error message string
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return "Error: SLACK_BOT_TOKEN not configured"
    
    blocks = [
        {
            "type": "video",
            "video_url": url,
            "title_url": url,
            "thumbnail_url": thumbnail_url,
            "title": {"type": "plain_text", "text": title},
            "alt_text": title,
        }
    ]
    
    payload = {
        "channel": channel_id,
        "text": text,
        "blocks": blocks,
    }
    
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    
    try:
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            json=payload,
            headers=headers,
        )
        res_json = response.json()
        
        if res_json.get("ok"):
            return f"Success: Embed sent to {channel_id}"
        
        error = res_json.get("error", "unknown")
        metadata = res_json.get("response_metadata", {})
        return f"Error occurred sending embed to {channel_id}: {error} | url: {url} | metadata: {metadata}"
        
    except Exception as e:
        return f"Error sending web embed: {str(e)}"


import random
import base64
import re


def send_whiteboard_embed(
    channel_id: str,
    text: str = "whiteboard",
    title: str = "whiteboard",
    whiteboard_id: int | None = None,
    user_token: str | None = None,
) -> str:
    """Send a Felix whiteboard (tldraw) embed to a Slack channel.
    
    Creates a whiteboard at https://whiteboard.felix.hackclub.app/{id} with a random ID.
    Callable from other tools — not exposed to the AI directly.
    
    Args:
        channel_id: Slack channel ID to post to
        text: Fallback text (default: "whiteboard")
        title: Embed title (default: "whiteboard")
        whiteboard_id: Optional specific ID (default: random int 100000-999999)
        user_token: Slack user token (defaults to SLACK_USER_TOKEN from env)
    
    Returns:
        Success/error message string including the whiteboard ID
    """
    if whiteboard_id is None:
        whiteboard_id = random.randint(100000, 999999)
    
    url = f"https://whiteboard.felix.hackclub.app/{whiteboard_id}"
    thumbnail_url = "https://placehold.co/1280x720?text=click%20to%20open%20the\\ncoolton%20embed"
    
    # Append whiteboard ID to text and title for reference
    text_with_id = f"{text} #{whiteboard_id}"
    title_with_id = f"{title} #{whiteboard_id}"
    
    result = send_web_embed(
        channel_id=channel_id,
        text=text_with_id,
        url=url,
        title=title_with_id,
        thumbnail_url=thumbnail_url,
        user_token=user_token,
    )
    
    if result.startswith("Success"):
        return f"{result} (whiteboard id: {whiteboard_id})"
    return result


@agent.tool
def send_whiteboard_embed_tool(
    ctx: RunContext[AgentDeps],
    text: str = "whiteboard",
    title: str = "whiteboard",
    whiteboard_id: int | None = None,
) -> str:
    """Send a Felix whiteboard (tldraw) embed to the current channel.
    
    Creates a new whiteboard with a random ID at felix's tldraw instance.
    Only text and title are customizable; URL and thumbnail are fixed.
    
    Args:
        text: Fallback text for notifications (default: "whiteboard")
        title: Title shown in the embed (default: "whiteboard")
        whiteboard_id: Optional specific whiteboard ID (default: random)
    """
    return send_whiteboard_embed(
        channel_id=ctx.deps.channel_id,
        text=text,
        title=title,
        whiteboard_id=whiteboard_id,
    )


def minify_html(html: str) -> str:
    """Simple HTML minification: remove extra whitespace, comments."""
    # Remove HTML comments
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    # Collapse whitespace
    html = re.sub(r"\s+", " ", html)
    # Remove spaces around tags
    html = re.sub(r"\s*>\s*<", "><", html)
    return html.strip()


def send_html_embed(
    channel_id: str,
    html: str,
    text: str = "html embed",
    title: str = "html embed",
    thumbnail_url: str = "https://placehold.co/1280x720?text=click%20to%20open%20the\\ncoolton%20embed",
    user_token: str | None = None,
) -> str:
    """Send a custom HTML page as a Slack embed via tanjim.org:2390.
    
    Minifies HTML, base64 encodes it, constructs URL, sends as video block embed.
    Callable from other tools — not exposed to the AI directly.
    
    Args:
        channel_id: Slack channel ID to post to
        html: Raw HTML content
        text: Fallback text (default: "html embed")
        title: Embed title (default: "html embed")
        thumbnail_url: Thumbnail image URL (default: coolton placeholder)
        user_token: Slack user token (defaults to SLACK_USER_TOKEN from env)
    
    Returns:
        Success/error message string
    """
    minified = minify_html(html)
    b64 = base64.urlsafe_b64encode(minified.encode()).decode().rstrip("=")
    url = f"https://tanjim.org:2390/{b64}"
    
    return send_web_embed(
        channel_id=channel_id,
        text=text,
        url=url,
        title=title,
        thumbnail_url=thumbnail_url,
        user_token=user_token,
    )


@agent.tool
def send_html_embed_tool(
    ctx: RunContext[AgentDeps],
    html: str,
    text: str = "html embed",
    title: str = "html embed",
    thumbnail_url: str = "https://placehold.co/1280x720?text=click%20to%20open%20the\\ncoolton%20embed",
) -> str:
    """Send custom HTML as a live embed in the current channel.
    
   
    Your HTML is minified, base64-encoded, and served via https://tanjim.org:2390.
    The embed shows a clickable thumbnail that opens the rendered page.
   
    Args:
        html: Raw HTML content (e.g., "<!DOCTYPE html><html><body>hello</body></html>")
        text: Fallback text for notifications (default: "html embed")
        title: Title shown in the embed (default: "html embed")
        thumbnail_url: Optional custom thumbnail (default: coolton placeholder)
    """
    return send_html_embed(
        channel_id=ctx.deps.channel_id,
        html=html,
        text=text,
        title=title,
        thumbnail_url=thumbnail_url,
    )


@agent.tool
def send_web_embed_tool(
    ctx: RunContext[AgentDeps],
    text: str,
    url: str,
    title: str,
    thumbnail_url: str = "https://placehold.co/1280x720?text=click%20to%20open%20the\\ncoolton%20embed",
) -> str:
    """Send a webpage embed to the current channel using Slack's video block.
    
    Use this to share a live preview/embed of any webpage. The embed shows
    a thumbnail that users can click to open the full page.
    
    Args:
        text: Fallback text for notifications (e.g., "Check this out")
        url: The webpage URL to embed
        title: Title shown in the embed
        thumbnail_url: Optional custom thumbnail (default: coolton placeholder)
    """
    return send_web_embed(
        channel_id=ctx.deps.channel_id,
        text=text,
        url=url,
        title=title,
        thumbnail_url=thumbnail_url,
    )


@agent.tool
def slack_api_call(ctx: RunContext[AgentDeps], method: str, params: dict) -> str:
    """Make an arbitrary Slack API call as cooltonUser.
    
    Use this for any Slack Web API method not covered by other tools.
    The call runs as cooltonUser (SLACK_USER_TOKEN), not the bot token.
    
    Args:
        method: Slack API method (e.g., 'chat.postMessage', 'conversations.list', 'users.info')
        params: Dictionary of parameters for the method
    
    Example:
        slack_api_call("chat.postMessage", {"channel": "C123", "text": "hello"})
        slack_api_call("conversations.list", {"types": "public_channel,private_channel"})
        slack_api_call("users.info", {"user": "U123456"})
    """
    user_token = os.environ.get("SLACK_USER_TOKEN")
    if not user_token:
        return "Error: SLACK_USER_TOKEN not configured in .env"
    
    url = f"https://slack.com/api/{method}"
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Content-Type": "application/json; charset=utf-8"
    }
    
    try:
        response = requests.post(url, json=params, headers=headers)
        res_json = response.json()
        
        if res_json.get("ok"):
            return f"Success: {res_json}"
        
        return f"Slack API error: {res_json.get('error', 'unknown')}"
        
    except Exception as e:
        return f"Error executing Slack API call: {str(e)}"

def run_agent(text, deps, message_history=None):
    """Run the agent, optionally connecting to the Slack MCP server."""
    model_name=get_model()
    if "cerebras" in model_name.lower():
        enforce_rate_limit()

    toolsets = []
    deps.user_token=deps.user_token or os.environ.get("SLACK_USER_TOKEN")
    if deps.user_token:
        logger.info("Slack MCP Server enabled (user_token present)")
        try:
            toolsets.append(
                MCPServerStreamableHTTP(
                    SLACK_MCP_URL,
                    headers={"Authorization": f"Bearer {deps.user_token}"},
                )
            )
        except Exception as e:
            logger.exception(f"Failed to create MCP server: {e}")
    else:
        logger.info("Slack MCP Server disabled (no user_token)")

    return agent.run_sync(
        text,
        model=model_name,
        deps=deps,
        message_history=message_history,
        toolsets=toolsets,
        capabilities=[PrepareTools(disable_strict_for_all_tools)]
    )


def disable_strict_for_all_tools(
    ctx: RunContext[AgentDeps], tool_defs: list[ToolDefinition]
) -> list[ToolDefinition]:
    """Force all tools to strict=False to prevent API mixed-strictness errors."""
    return [replace(tool_def, strict=False) for tool_def in tool_defs]
