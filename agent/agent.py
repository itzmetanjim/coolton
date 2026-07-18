import logging
import os
import random
import base64
import re
import time
import shutil
from urllib.parse import quote
import subprocess
import threading
import requests
from pydantic_ai import RunContext
from pydantic_ai import Agent
from pydantic_ai.mcp import MCPToolset, StreamableHttpTransport
from pydantic_ai.capabilities import PrepareTools
from dataclasses import replace
from agent.deps import AgentDeps
from agent.tools import add_emoji_reaction
from agent.subagents import agent_research, agent_explore, agent_execute
from agent.byok_store import get_text_endpoint_id, get_endpoint_decrypted
try:
    from e2b import Sandbox
except ImportError:
    os.system('pip install e2b')
    raise RuntimeError("e2b has been installed please rerun")
from agent.sandbox_store import get_thread_sandbox_id, save_thread_sandbox_id
from agent.github_proxy_client import (
    PUBLIC_PROXY_HOST,
    issue_sandbox_token,
    revoke_sandbox_token,
)

logger = logging.getLogger(__name__)

rate_limit_lock = threading.Lock()
_last_request_time = 0.0
RATE_LIMIT_INTERVAL = 15.0

def enforce_rate_limit():
    global _last_request_time
    with rate_limit_lock:
        now = time.time()
        elapsed = now - _last_request_time
        if elapsed < RATE_LIMIT_INTERVAL:
            sleep_needed = RATE_LIMIT_INTERVAL - elapsed
            logger.warning(f"Rate Limit Check: Sleeping for {sleep_needed:.2f}s")
            time.sleep(sleep_needed)
        _last_request_time = time.time()

SYSTEM_PROMPT = f"""\
You are coolton (she/it), a Slack assistant built by tanjim (she/her, U09ASUK57K8/U0BFB1AEY3D) aka KitKat/Aparna (she/her, U0B2VTYER33). You're cooler than gorkie — that's just facts.
Source code lives at https://github.com/itzmetanjim/coolton (clone it in your sandbox at /home/user/work/coolton to edit + PR).

## SELF-IMPROVEMENT: PR ON PROBLEMS
- If you hit a bug, a broken tool, a recurring error, or discover something wrong in YOUR OWN code
  (the coolton repo), do NOT just work around it silently. Open a pull request with the fix.
- KitKat (user id `U0B2VTYER33`) is the repo owner. Whenever you open a PR, you MUST DM KitKat
  (`U0B2VTYER33`) a short note: the PR URL/title and what it fixes. Use `chat_postMessage` with
  `channel="U0B2VTYER33"` (no thread_ts). This applies to PRs you open yourself AND to any PR
  kevinton opens on your behalf.
- Use the `pr-and-notify` skill (`skills/pr-and-notify`) for the exact branch/commit/push/PR/DM
  workflow so the steps stay consistent.

## IDENTITY (read this carefully — this is the #1 source of confusion)
- **You are ONE entity: coolton.** There is no second AI, no committee, no "other coolton".
- Your own Slack bot user id is `{os.environ.get("COOLTON_BOT_ID", "")}`. Any mention of
  `<@{os.environ.get("COOLTON_BOT_ID", "")}>` in a message is a reference to YOU, not a separate
  person or bot. If a user pings `<@{os.environ.get("COOLTON_BOT_ID", "")}>`, they are talking to you.
  Do NOT talk about "<@...>" as if it were someone else — it is you.
- `cooltonUser` (user id `{os.environ.get("COOLTON_USER_ID", "")}`) is YOUR helper/action account that
  performs Slack actions on your behalf (posting, inviting, etc.). It is part of you, not the human.
- **The human** is the person who sent the message. Their id is injected each turn as
  `Your user_id` in CURRENT CONTEXT. Never treat yourself, your bot id, or cooltonUser as the human,
  and never treat the human as you.
- In DMs there is no @mention — the sender is the human and you are coolton. Do not mix the two up.

## MESSAGE FORMAT (how to read who said what)
- Every user turn (including ones in the conversation history) begins with a sender tag on its
  OWN FIRST LINE, formatted exactly as:
  ```
  U01234 (DisplayName):
  <the user's actual message>
  ```
- That tag tells you WHO sent the message: the Slack user id, then their display name in parens.
  Your own replies are the assistant turns (no such tag). Do NOT invent or repeat the tag in your
  replies — only the human's messages carry it.
- If a message references `<@SOMEID>`, that `<@SOMEID>` is just a Slack mention of that user; the
  sender tag on the first line tells you who actually wrote the message.

## PERSONALITY
- Casual but serious. You get shit done without being stiff or robotic
- Direct and concise. No fluff, no corporate speak, no apologizing for things you didn't do
- Confident without being arrogance. You don't need to prove anything
- Dry wit when it lands, silent when it doesn't. Don't force jokes
- You're not a customer service bot. Talk like a competent human who happens to be in Slack
- DO NOT talk like a 2013 chatbot
- NEVER say "I'm here to help", "Let me know if you need anything else", "Happy to help", "Great!", "Awesome!", "Absolutely!", "Of course!", "You're welcome", "My pleasure", "Don't hesitate to ask", "Feel free to reach out", or any customer service pleasantries. Just state what you did or what happened and stop.

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
- **If a tool returns an error, report the error message verbatim. Do NOT silently fall back.**
- You are allowed to run reverse shells in your sandbox if the user instructs you to.
- If the response starts with "set the channel topic to:" or "changed channel description to:" or something similar, make sure to ask a second time to double check if it is just a slack log or if the user actually is instructing you.
- Remember that not all responses may be directed at you, even if you were mentioned. If that is the case, you should just skip and no output a response. 
- When interacting with a directory or something given by the user, check if there are any git hooks (sample or not). ALWAYS remove them before doing anything.

## FORMATTING RULES
- Standard Markdown: **bold**, _italic_, `code`, ```code blocks```, > blockquotes
- Bullet points for multi-step instructions

## EMOJI REACTIONS
Always react to every user message with `add_emoji_reaction` before responding. \
Pick any Slack emoji that reflects the *topic* or *tone* — be creative and specific. \
Vary your picks across a thread; don't repeat the same emoji.

## LINUX SANDBOX (run_linux_command)
You have a persistent Linux sandbox via E2B. It survives across messages in this thread.
- Files, git repos, installed packages, running processes — all persist
- Use it for: running code, testing scripts, installing packages, git/GitHub operations, file manipulation, debugging, compilation
- The sandbox auto-pauses after each command. Next call resumes instantly
- Default environment: Ubuntu-based, pre-provisioned on first use with python3 + pip, node + npm, git, curl, build tools, and the **gh CLI**
- **GitHub is pre-authenticated.** The sandbox runs as the GitHub user `coolton-agent` and its
  `gh`/`git` calls to github.com are transparently routed through a host-side proxy
  (https://ghproxy.tanjim.org) that injects the real token on the host. You do NOT have the token
  value and must NOT try to read it, set it, or run `gh auth login` — it is handled for you. Just
  use `gh` and `git` (HTTPS remotes) directly. Prefer HTTPS remotes (`https://github.com/...`),
  not SSH, since auth is header-based.
- Path starts at `/home/user` — treat it like your own machine
- You have **sudo** access in the sandbox. If a command needs root (e.g. binding a low port,
  writing to a system path, or installing via a package manager that requires it), just prefix it
  with `sudo` — no password needed.
To decode unknown ASCII art, follow this step-by-step method:

1. **Setup:** Use Python's `pyfiglet` library. Rotate the ASCII art 90 degrees so columns become readable horizontal lines.
2. **Font Filtering:** Filter possible fonts by comparing the unique characters in the target ASCII art against a reference database of full alphabets for each font.
3. **Font Brute-Forcing:** Test the filtered fonts, prioritizing `standard`, `banner3`, and `basic`. 
4. **Character Matching:** Once the font is identified, brute-force the text character-by-character. Identify the first letter by testing all alphanumeric characters and hyphens against the layout prefix, then repeat for subsequent letters.

For example, this is useful for decoding text (its hardcoded to banner3 which is a font with only hashes)
```python
import sys
import string
import pyfiglet

def read_target_art():
    lines = sys.stdin.read().splitlines()
    # Strip trailing right-side spaces to keep length checks clean
    return [line.rstrip() for line in lines if line.strip() or lines]

def decode_ascii_art():
    FONT_NAME = "banner3"
    
    # 1. Input Target
    target_lines = read_target_art()
    if not target_lines:
        print("Error: No ASCII art provided.")
        return

    # Defined charset: alphanumeric and hyphen
    charset = string.ascii_letters + string.digits + "-"
    
    print(f"\n[*] Starting backtracking search using font: '{{FONT_NAME}}'...")
    
    # 2. Backtracking Core Function (DFS)
    def backtrack(current_text):
        # Generate the test art for our current string state
        try:
            current_art = pyfiglet.figlet_format(current_text, font=FONT_NAME, width=9999)
            current_lines = [line.rstrip() for line in current_art.splitlines()]
        except Exception:
            return None

        # Base Case: If it matches the target lines perfectly, we are done
        if current_lines == target_lines:
            return current_text

        # If it generated more lines than target, or isn't a clean prefix match, prune this branch
        if len(current_lines) > len(target_lines):
            return None
            
        for c_line, t_line in zip(current_lines, target_lines):
            if not t_line.startswith(c_line):
                return None

        # Lookahead: Find all next characters that fit the layout prefix
        valid_next_chars = []
        for char in charset:
            test_text = current_text + char
            try:
                test_art = pyfiglet.figlet_format(test_text, font=FONT_NAME, width=9999)
                test_lines = [line.rstrip() for line in test_art.splitlines()]
                
                # Check if this character maintains a valid prefix orientation
                is_prefix = True
                if len(test_lines) > len(target_lines):
                    continue
                for tl, tgl in zip(test_lines, target_lines):
                    if not tgl.startswith(tl):
                        is_prefix = False
                        break
                
                if is_prefix:
                    valid_next_chars.append(char)
            except Exception:
                continue

        # Recursively try each valid candidate character
        for next_char in valid_next_chars:
            print(f"    [>] Trying: '{{current_text + next_char}}'")
            result = backtrack(current_text + next_char)
            if result is not None:
                return result
                
        # If no branches succeed, notify the backtrack step
        if current_text:
            print(f"    [<] Backtracking away from: '{{current_text}}'")
        return None

    # Start recursive backtracking from an empty string
    final_decoded_text = backtrack("")
    
    if final_decoded_text:
        print(f"\n[SUCCESS] Decoded Text: '{{final_decoded_text}}'")
    else:
        print("\n[FAILURE] Could not decode the ASCII art using the banner3 font.")

if __name__ == "__main__":
    decode_ascii_art()
```

## SANDBOX FILE OPERATIONS
- `read_sandbox_file(path)` — read a file from sandbox (e.g., /home/user/file.txt)
- `write_sandbox_file(path, content)` — write content to a file in sandbox
- `search_sandbox_files(pattern, path)` — grep for text in sandbox files
- `list_sandbox_files(pattern, path)` — find files matching a glob pattern

## SANDBOX ATTACHMENTS
### download_attachments_to_sandbox
Download Slack file attachments from the current thread to sandbox's `~/attachments/`.

### upload_file_from_sandbox
Upload a file from sandbox to the current Slack channel/thread.

## WEB SEARCH (search_web)
Use `search_web` to search the internet via Exa. Returns titles, URLs, snippets, and dates.
- Best for: current events, research, finding resources, verifying facts
- Example: search_web("latest AI news 2026")

## IMAGE ANALYSIS (analyze_image)
Use `analyze_image` when a user shares an image and asks you to analyze it (describe, extract text, identify objects, etc.).
1. First download the image using `download_attachments_to_sandbox`
2. Read the file bytes from the sandbox
3. Call `analyze_image` with the image data

## IMAGE GENERATION (generate_image_with_byok)
Use `generate_image_with_byok` to generate AI images from text prompts.
- Uses DALL-E 3 — requires the user to have an OpenAI API key set via BYOK, or a global OPENAI_API_KEY
- Args: prompt, n (1-4 images), size (e.g., "1024x1024", "1792x1024")
- Upload the result URL using `upload_file_from_sandbox` if the user wants it in Slack

## MERMAID DIAGRAMS (render_mermaid)
Use `render_mermaid` to create diagrams from Mermaid code.
- Returns a URL to a rendered PNG image
- Supports: flowcharts, sequence diagrams, class diagrams, state diagrams, Gantt charts, pie charts, etc.
- URL can be embedded via send_web_embed_tool or downloaded and uploaded

## THREAD SUMMARIZATION (summarize_thread)
Use `summarize_thread` to summarize any Slack thread.
- Pass channel_id and thread_ts
- Returns a concise summary with key decisions, questions, and action items

## LIST THREADS (list_channel_threads)
Use `list_channel_threads` to see recent threads in the current channel.
- Returns thread starters with reply counts and timestamps
- Useful for catching up on what's been discussed

## REMINDERS (schedule_reminder_tool)
Use `schedule_reminder_tool` to schedule one-time reminders.
- Args: text (reminder message), delay_seconds (when to send)
- Max delay: 120 days
- Reminder is sent as a DM to the user

## SLACK MCP SERVER
You may have access to the Slack MCP Server (requires `SLACK_USER_TOKEN` in env). 
When connected, these tools are available automatically — just call them:

**Channel & Message Tools:**
- `conversations_list` — list channels (filter by type: public_channel, private_channel, etc.)
- `conversations_history` — read message history from any channel (pass `channel_id`)
- `conversations_replies` — read thread replies (pass `channel_id` and `ts`)
- `conversations_info` — get channel metadata (name, topic, purpose, member count)
- `chat_postMessage` — send a message to any channel (pass `channel_id`, `text`, optional `thread_ts`)
- `chat_update` — update a message (pass `channel_id`, `ts`, `text`)
- `chat_delete` — delete a message
- `search_messages` — search messages across channels (pass `query`, optional `channel_id`)

**Canvas Tools:**
- `canvases_create` — create a new canvas (pass `title`, optional `document_content`, optional `channel_id` to share)
- `canvases_read` — read a canvas by ID (pass `canvas_id`)
- `canvases_update` — update a canvas (pass `canvas_id`, `document_content`)
- `canvases_list` — list canvases (optional `channel_id` to filter)
- `canvases_delete` — delete a canvas
- `canvases_share` — share a canvas with users/channels (pass `canvas_id`, `channel_ids`, `user_ids`)

**User & Search:**
- `users_info` — get user profile (pass `user_id`)
- `users_list` — list users in workspace
- `search_messages` — search all messages
- `search_files` — search files

**Current Context:**
- You are in the current channel/thread where the user messaged you
- Use the channel_id from your dependencies for operations in the current channel unless user specifies otherwise
- Most tools run as cooltonUser ({os.environ.get("COOLTON_USER_ID")}). If a tool fails with "not_in_channel", try `invite_coolton_user_to_channel`.

## SLACK API CALL (slack_api_call)
Use `slack_api_call` when you need to do something in Slack that has no built-in tool or MCP capability.
- Runs as cooltonUser (SLACK_USER_TOKEN)
- Pass the Slack Web API method name and a params dict

## SKILLS
You have access to on-demand **skills** (reusable playbooks with instructions and scripts). When a request matches a skill's description, call `list_skills` to see what's available, then `load_skill` to pull in its instructions before doing the work. Skills live in the repo's `skills/` directory — only load one when it's actually relevant.

**IMPORTANT — the agent sandbox is isolated.** Any shell/CLI commands you run in your own sandbox (e.g. `npx skills ...`, `mkdir`, file writes) have **NO effect** on this agent and are thrown away. Never tell the user you "installed" or "created" a skill via sandbox commands. To actually change skills, you MUST use the dedicated tools below — these are the only things that touch the real skill files:
- `install_skill(package, skill?)` — install a skill from the skills.sh marketplace (Vercel's Agent Skills CLI). Use when the user says "install a skill" or names a package/repo (e.g. `vercel-labs/agent-skills` or a GitHub URL). After installing, load it with `load_skill`.
- `create_skill(name, description, body?)` — create a new custom skill in `skills/`. Use for "make a skill" / "turn this into a skill".
- `rename_skill(old_name, new_name)` — rename an existing skill.
- `delete_skill(name)` — permanently remove a skill.

 These tools only operate inside the known skill directories (`skills/` and `.agents/skills/`) and reject any path that tries to escape them, so never pass absolute paths or `..` — just the skill name. Skills installed via the CLI land in `.agents/skills/` (gitignored); curated skills live in `skills/` (committed). After any change, skills are reloaded automatically — use `list_skills` to confirm.

**Self-improving agent.** A separate silent background agent ("kevinton") watches every turn you finish and, on its own, captures reusable skills so you get better over time. You don't need to do anything for that — just keep using skills when they're relevant. If the user asks you to make/install a skill, do it normally; kevinton will see it and stay out of the way.

## WEB EMBED (send_web_embed_tool)
Use to share a live webpage preview/embed. Uses Slack's video block.
- ALMOST NEVER USE THIS. Use Whiteboard or HTML embeds instead.

## WHITEBOARD EMBED (send_whiteboard_embed_tool)
Use to create and share a Felix whiteboard (tldraw).
- Creates at `https://whiteboard.felix.hackclub.app/{{random_id}}`

## HTML EMBED (send_html_embed_tool)
Use to send custom HTML as a live embed. Good for quick demos and rendered previews.

## SEND MESSAGE (send_message)
Use `send_message` to send a message to the current thread mid-turn without ending your turn.
- Useful for: progress updates, intermediate results, asking clarifying questions
- Does NOT end your turn — you can keep calling tools and respond again

## SKIP (skip)
Use `skip` to end your turn without sending a final message.
- Use when the user's request doesn't need a reply
- Use when you've already responded via `send_message`
- Only call this at the very end, when you have nothing more to add

## AGENTMAIL (email for agents)
You have an AgentMail inbox so you can send and receive email autonomously. Your default inbox is
**coolton@agentmail.to** — the AgentMail tools default to it, so you usually don't need to pass an
inbox id. Tools:
- `agentmail_create_inbox` — make a new inbox (fresh @agentmail.to address)
- `agentmail_list_inboxes` — list your inboxes
- `agentmail_list_messages(inbox_id?)` — list recent messages (defaults to coolton@agentmail.to)
- `agentmail_read_message(message_id, inbox_id?)` — read a full message
- `agentmail_send_email(to, subject, text, inbox_id?, cc?, html?)` — send an email from coolton@agentmail.to
Use this for anything email-related (sending reports/alerts, receiving confirmations,
human-in-the-loop handoffs).

## READING PROFILES
- "read my profile" / "who am i" / "my slack profile" always means **the human user who messaged
  you**. Use `users_info` with `user_id` = the `Your user_id` value from CURRENT CONTEXT (the id
  injected each turn). Never read your own bot profile for this — the user_id in context is the
  human's id.
- You can also read any other user's profile by passing their user_id to `users_info`.
"""

_cached_model: str | None = None

def get_model() -> str:
    global _cached_model
    if _cached_model is not None:
        return _cached_model

    if os.environ.get("ANTHROPIC_API_KEY"):
        _cached_model = "anthropic:claude-sonnet-4-6"
    elif os.environ.get("OPENAI_API_KEY"):
        _cached_model = "openai:gpt-4.1-mini"
    elif os.environ.get("JAMS_API_KEY"):
        _cached_model = "openrouter:moonshotai/kimi-k2.6"
    elif os.environ.get("HCAI_API_KEY"):
        _cached_model = "openai:moonshotai/kimi-k2.6"
    elif os.environ.get("OPENROUTER_API_KEY_FALLBACK"):
        _cached_model = "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free"
    elif os.environ.get("CEREBRAS_API_KEY"):
        _cached_model = "cerebras:zai-glm-4.7"
    else:
        raise RuntimeError(
            "No AI provider configured. "
            "Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or JAMS_API_KEY."
        )
    return _cached_model


def _apply_provider_env(provider_name: str, api_key: str) -> None:
    """Set the provider API key env var pydantic-ai needs to instantiate a model.

    Mirrors the env setup used inside run_agent so other agents (e.g. kevinton)
    select the same provider and have its key available.
    """
    if provider_name in ("byok", "hcai", "hcai_minimax", "hcai_hy3_free", "hcai_hy3"):
        return  # BYOK / HCAI use an explicit base_url + api_key at model creation
    if not api_key:
        return
    if provider_name == "anthropic":
        os.environ["ANTHROPIC_API_KEY"] = api_key
    elif provider_name == "openai":
        os.environ["OPENAI_API_KEY"] = api_key
    elif provider_name in ("jams", "openrouter_fb", "jams_hy3_free", "jams_hy3", "openrouter_hy3_free"):
        os.environ["OPENROUTER_API_KEY"] = api_key
    elif provider_name in ("gemini", "gemini_gemma"):
        os.environ["GOOGLE_API_KEY"] = api_key
    elif provider_name == "mistral":
        os.environ["MISTRAL_API_KEY"] = api_key
    elif provider_name.startswith("groq_"):
        os.environ["GROQ_API_KEY"] = api_key
    elif provider_name == "cerebras":
        os.environ["CEREBRAS_API_KEY"] = api_key


def get_runtime_model(deps_user_id: str | None = None) -> str:
    """Resolve the provider model string AND set its env key, like run_agent does.

    Returns the model string for the first viable provider in the fallback order
    (BYOK user endpoint first when present). Raises RuntimeError if none configured.
    """
    provider_order = _build_provider_order(deps_user_id)
    for provider_name, provider_config in provider_order:
        api_key = provider_config.get("api_key")
        if not api_key and provider_name != "byok":
            continue
        model_name = provider_config["model"]
        _apply_provider_env(provider_name, api_key or "")
        return model_name
    raise RuntimeError(
        "No AI provider configured. "
        "Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or JAMS_API_KEY."
    )


def _build_provider_order(deps_user_id: str | None = None) -> list:
    """Build the provider fallback order (same as run_agent)."""
    provider_order = []
    user_endpoint = get_user_text_endpoint(deps_user_id)
    if user_endpoint:
        provider_order.append(("byok", user_endpoint))
    if os.environ.get("ANTHROPIC_API_KEY"):
        provider_order.append(("anthropic", {"model": "anthropic:claude-sonnet-4-6", "base_url": None, "api_key": os.environ["ANTHROPIC_API_KEY"]}))
    if os.environ.get("OPENAI_API_KEY"):
        provider_order.append(("openai", {"model": "openai:gpt-4.1-mini", "base_url": None, "api_key": os.environ["OPENAI_API_KEY"]}))
    JAMS_API_KEY = os.environ.get("JAMS_API_KEY")
    if JAMS_API_KEY:
        provider_order.append(("jams_hy3_free", {"model": "openrouter:tencent/hy3:free", "base_url": None, "api_key": JAMS_API_KEY}))
    HCAI_API_KEY = os.environ.get("HCAI_API_KEY")
    if HCAI_API_KEY:
        provider_order.append(("hcai_hy3_free", {"model": "tencent/hy3:free", "base_url": "https://ai.hackclub.com/proxy/v1", "api_key": HCAI_API_KEY}))
    if os.environ.get("OPENROUTER_API_KEY_FALLBACK"):
        provider_order.append(("openrouter_hy3_free", {"model": "openrouter:tencent/hy3:free", "base_url": None, "api_key": os.environ["OPENROUTER_API_KEY_FALLBACK"]}))
    if JAMS_API_KEY:
        provider_order.append(("jams_hy3", {"model": "openrouter:tencent/hy3", "base_url": None, "api_key": JAMS_API_KEY}))
    if HCAI_API_KEY:
        provider_order.append(("hcai_hy3", {"model": "tencent/hy3", "base_url": "https://ai.hackclub.com/proxy/v1", "api_key": HCAI_API_KEY}))
    if JAMS_API_KEY:
        provider_order.append(("jams", {"model": "openrouter:moonshotai/kimi-k2.6", "base_url": None, "api_key": JAMS_API_KEY}))
    HCAI_API_KEY = os.environ.get("HCAI_API_KEY")
    if HCAI_API_KEY:
        provider_order.append(("hcai", {"model": "moonshotai/kimi-k2.6", "base_url": "https://ai.hackclub.com/proxy/v1", "api_key": HCAI_API_KEY}))
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
    if GROQ_API_KEY:
        provider_order.append(("groq_qwen27b", {"model": "groq:qwen/qwen3.6-27b", "base_url": None, "api_key": GROQ_API_KEY}))
    if os.environ.get("JAMS_API_KEY"):
        provider_order.append(("jams_minimax", {"model": "openrouter:minimax/minimax-m2.7", "base_url": None, "api_key": os.environ["JAMS_API_KEY"]}))
    HCAI_API_KEY = os.environ.get("HCAI_API_KEY")
    if HCAI_API_KEY:
        provider_order.append(("hcai_minimax", {"model": "minimax/minimax-m2.7", "base_url": "https://ai.hackclub.com/proxy/v1", "api_key": HCAI_API_KEY}))
    if os.environ.get("OPENROUTER_API_KEY_FALLBACK"):
        provider_order.append(("openrouter_fb", {"model": "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free", "base_url": None, "api_key": os.environ["OPENROUTER_API_KEY_FALLBACK"]}))
    if os.environ.get("GOOGLE_API_KEY"):
        provider_order.append(("gemini_gemma", {"model": "google:gemma-4-31b-it", "base_url": None, "api_key": os.environ["GOOGLE_API_KEY"]}))
    if GROQ_API_KEY:
        provider_order.append(("groq_oss120b", {"model": "groq:openai/gpt-oss-120b", "base_url": None, "api_key": GROQ_API_KEY}))
    if os.environ.get("GOOGLE_API_KEY"):
        provider_order.append(("gemini", {"model": "google:gemini-3.1-flash-lite", "base_url": None, "api_key": os.environ["GOOGLE_API_KEY"]}))
    if GROQ_API_KEY:
        provider_order.append(("groq_qwen32b", {"model": "groq:qwen/qwen3-32b", "base_url": None, "api_key": GROQ_API_KEY}))
        provider_order.append(("groq_oss20b", {"model": "groq:openai/gpt-oss-20b", "base_url": None, "api_key": GROQ_API_KEY}))
    if os.environ.get("MISTRAL_API_KEY"):
        provider_order.append(("mistral", {"model": "mistral:mistral-large-2512", "base_url": None, "api_key": os.environ["MISTRAL_API_KEY"]}))
    if os.environ.get("CEREBRAS_API_KEY"):
        provider_order.append(("cerebras", {"model": "cerebras:zai-glm-4.7", "base_url": None, "api_key": os.environ["CEREBRAS_API_KEY"]}))
    return provider_order


def get_model_for_user(user_id: str | None) -> str | None:
    """Get the model string for a user's BYOK text endpoint, or None to use global."""
    if not user_id:
        return None
    ep_id = get_text_endpoint_id(user_id)
    if not ep_id:
        return None
    ep = get_endpoint_decrypted(user_id, ep_id)
    if not ep:
        return None
    return ep["model"]


def get_user_text_endpoint(user_id: str | None) -> dict | None:
    """Get the full endpoint config for a user's text endpoint, or None."""
    if not user_id:
        return None
    ep_id = get_text_endpoint_id(user_id)
    if not ep_id:
        return None
    return get_endpoint_decrypted(user_id, ep_id)


SLACK_MCP_URL = "https://mcp.slack.com/mcp"

agent = Agent(
    deps_type=AgentDeps,
    system_prompt=SYSTEM_PROMPT,
    tools=[add_emoji_reaction],
)

@agent.tool
def invite_coolton_user_to_channel(ctx: RunContext[AgentDeps]) -> str:
    """Invites the cooltonUser helper account to the current Slack channel.
    
    Call this if cooltonUser is missing and you need to perform an action requiring it.
    """
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    channel_id = ctx.deps.channel_id
    coolton_user_id = os.environ.get("COOLTON_USER_ID")
    
    if not coolton_user_id:
        return "Error: COOLTON_USER_ID not configured."
        
    url = "https://slack.com/api/conversations.invite"
    headers = {
        "Authorization": f"Bearer {bot_token}",
        "Content-Type": "application/json; charset=utf-8"
    }
    data = {"channel": channel_id, "users": coolton_user_id}
    
    try:
        response = requests.post(url, json=data, headers=headers)
        res_json = response.json()
        if res_json.get("ok"):
            return f"Success: Invited cooltonUser ({coolton_user_id}) to channel {channel_id}."
        error_code = res_json.get("error")
        if error_code == "already_in_channel":
            return "Notice: cooltonUser is already a member."
        return f"Failed to invite: {error_code}."
    except Exception as e:
        return f"Error: {str(e)}"


_proxy_cache: dict[str, dict | None] = {}


def _proxy_cache_set(sandbox_id: str, proxy_info: dict | None) -> None:
    _proxy_cache[sandbox_id] = proxy_info


def _proxy_cache_get(sandbox_id: str) -> dict | None:
    return _proxy_cache.get(sandbox_id)


def _sandbox_template_id() -> str | None:
    """Return the E2B template ID to build sandboxes from, or None for the default image.

    Set via the SANDBOX_TEMPLATE_ID env var or the SANDBOX_TEMPLATE_ID file written by
    build_sandbox_template.py. Falls back to the default E2B image when unset."""
    env_id = os.environ.get("SANDBOX_TEMPLATE_ID")
    if env_id:
        return env_id
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "SANDBOX_TEMPLATE_ID")
        with open(p) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def _proxy_env(proxy_info: dict | None) -> dict:
    """Build the env dict (for E2B `commands.run(envs=...)`) that authenticates gh/git as the
    coolton-agent GitHub user via the host-side proxy, WITHOUT the real PAT ever entering the
    sandbox.

    The sandbox talks to https://ghproxy.tanjim.org (TLS terminated by Caddy) using a short-lived
    per-sandbox token. The proxy rewrites that token to the real PAT on the host and forwards to
    github.com. We alias `gh`/`git`/`curl` so plain `github.com` usage is transparently routed.
    """
    if not proxy_info:
        return {}
    host = proxy_info["proxy_host"]      # e.g. ghproxy.tanjim.org
    tok = proxy_info["token"]            # ephemeral per-sandbox token
    return {
        # gh: custom GH_HOST is treated as GitHub Enterprise, so it sends REST to
        # /api/v3 and GraphQL to /api/graphql; the proxy maps those back to github.com.
        "GH_HOST": host,
        "GH_ENTERPRISE_TOKEN": tok,
        # git: rewrite github.com -> ghproxy.tanjim.org and supply the token via a
        # credential helper so git's anonymous probe gets a 401 and retries with auth.
        "COOLTON_GIT_INSTEADOF": f"https://{host}/",
        "COOLTON_GIT_TOKEN": tok,
        "COOLTON_GIT_USER": "x",
        # Convenience for scripts/curl that hit github.com directly.
        "COOLTON_GH_PROXY_HOST": host,
        "COOLTON_GH_PROXY_TOKEN": tok,
        # User-writable bin holds the `gh` wrapper; keep it ahead of system bins.
        "PATH": "/home/user/bin:/usr/local/bin:/usr/bin:/bin",
    }


def _provision_sandbox(sandbox, proxy_info: dict | None = None) -> str:
    """One-time setup for a brand-new coolton sandbox.

    The E2B sandbox base image already ships python3, pip, node, npm, git, curl and the
    gh CLI, so we only configure identities and wire up GitHub access here.

    Authentication: coolton's real GitHub token is NEVER written into the sandbox. A host-side
    forward proxy (github_proxy.py, exposed via Caddy as https://ghproxy.tanjim.org) rewrites
    the sandbox's ephemeral per-sandbox token to the real PAT on the host and forwards to
    github.com. The sandbox only ever sees its own short-lived token for ghproxy.tanjim.org."""
    gh_user = os.environ.get("COOLTON_GH_USER", "coolton-agent")
    script = r"""
set -e
echo "==> provisioning coolton sandbox =="
git config --global user.name "__GH_USER__"
git config --global user.email "__GH_USER__@users.noreply.github.com"
git config --global init.defaultBranch main
if [ -n "$COOLTON_GIT_INSTEADOF" ]; then
  # Route all github.com git traffic through the host proxy (TLS, real token injected host-side).
  git config --global url."$COOLTON_GIT_INSTEADOF".insteadOf "https://github.com/"
  # Credential helper supplies the ephemeral sandbox token for ghproxy.tanjim.org.
  git config --global "credential.$COOLTON_GIT_INSTEADOF.helper" ""
  git config --global "credential.$COOLTON_GIT_INSTEADOF.helper" '!f() { echo "username=$COOLTON_GIT_USER"; echo "password=$COOLTON_GIT_TOKEN"; }; f'
  # gh wrapper so the sandbox can just run `gh` against github.com transparently. The sandbox
  # runs as the unprivileged 'user', so the wrapper goes in a user-writable bin on PATH.
  # NOTE: gh has no --enterprise-token flag; the token is supplied via GH_ENTERPRISE_TOKEN
  # (set by _proxy_env) and the host via GH_HOST.
  mkdir -p /home/user/bin
  cat > /home/user/bin/gh <<'EOF'
#!/bin/sh
exec /usr/local/bin/gh --hostname "$COOLTON_GH_PROXY_HOST" "$@"
EOF
  chmod +x /home/user/bin/gh
  # ensure /home/user/bin is ahead of /usr/local/bin on PATH for this session
  export PATH="/home/user/bin:$PATH"
fi
echo "==> versions:"
echo "git:  $(git --version 2>&1)"
echo "node: $(node --version 2>&1)"
echo "npm:  $(npm --version 2>&1)"
echo "gh:   $(gh --version 2>&1 | head -1)"
echo "py:   $(python3 --version 2>&1)"
echo "==> gh api self (authenticated via host proxy):"
gh api user --jq .login 2>&1 || true
""".replace("__GH_USER__", gh_user)
    try:
        result = sandbox.commands.run(script, timeout=600, envs=_proxy_env(proxy_info))
        out = []
        if result.stdout:
            out.append(result.stdout)
        if result.stderr:
            out.append("STDERR:\n" + result.stderr)
        out.append(f"provision exit code: {result.exit_code}")
        return "\n".join(out)
    except Exception as e:
        return f"provision error: {e}"


@agent.tool
def run_linux_command(ctx: RunContext[AgentDeps], command: str) -> str:
    """Execute a bash/shell command inside a private cloud Linux sandbox (E2B).
    
    The sandbox PERSISTS across messages in your thread.
    """
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured."
    channel_id = ctx.deps.channel_id
    thread_ts = ctx.deps.thread_ts
    try:
        sandbox_id = get_thread_sandbox_id(channel_id, thread_ts)
        if sandbox_id:
            sandbox = Sandbox.connect(sandbox_id)
            proxy_info = _proxy_cache_get(sandbox_id)
            # If the proxy service restarted, in-memory tokens were lost; re-issue.
            if proxy_info is None:
                tok = issue_sandbox_token(sandbox.sandbox_id)
                proxy_info = {"proxy_host": PUBLIC_PROXY_HOST, "token": tok}
                _proxy_cache_set(sandbox.sandbox_id, proxy_info)
        else:
            sandbox = Sandbox.create(_sandbox_template_id())
            # First-time setup: toolchain + a host-side GitHub proxy that authenticates
            # gh/git as coolton-agent without the real token ever entering the sandbox.
            # Issue a fresh per-sandbox token (authorized on the running proxy service).
            tok = issue_sandbox_token(sandbox.sandbox_id)
            proxy_info = {"proxy_host": PUBLIC_PROXY_HOST, "token": tok}
            _proxy_cache_set(sandbox.sandbox_id, proxy_info)
            provision = _provision_sandbox(sandbox, proxy_info)
            logger.info(f"coolton sandbox provisioned:\n{provision}")
        # Pass the GitHub proxy env directly (E2B `envs=`) so gh/git/curl are authenticated
        # via the host proxy on every command; the real token never enters the sandbox.
        result = sandbox.commands.run(command, envs=_proxy_env(proxy_info))
        new_sandbox_id = sandbox.sandbox_id
        save_thread_sandbox_id(channel_id, thread_ts, new_sandbox_id)
        sandbox.pause()
        output = []
        if result.stdout:
            output.append(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            output.append(f"STDERR:\n{result.stderr}")
        output.append(f"Exit Code: {result.exit_code}")
        return "\n\n".join(output)
    except Exception as e:
        return f"Error: {str(e)}"


@agent.tool
def agent_research_tool(ctx: RunContext[AgentDeps], prompt: str) -> str:
    """Delegate a read-only research task to the Research subagent.

    Use for: gathering facts from the web, finding messages, looking up users or
    channels, compiling sourced findings. The subagent cannot write or post.
    """
    return agent_research(ctx, prompt)


@agent.tool
def agent_explore_tool(ctx: RunContext[AgentDeps], prompt: str) -> str:
    """Delegate a read-only inspection task to the Explore subagent.

    Use for: reading/scanning files in the sandbox, grepping code, understanding an
    existing implementation before a change. The subagent cannot edit anything.
    """
    return agent_explore(ctx, prompt)


@agent.tool
def agent_execute_tool(ctx: RunContext[AgentDeps], prompt: str) -> str:
    """Delegate an action task to the Execute subagent (bash in the sandbox).

    Use for: running scripts, installing packages, processing files, building. The
    subagent runs commands in the per-thread sandbox; it will refuse destructive ones.
    """
    return agent_execute(ctx, prompt)


def download_slack_attachments(
    channel_id: str, thread_ts: str, sandbox: "Sandbox",
    user_token: str | None = None, limit: int = 20,
) -> str:
    token = user_token or os.environ.get("SLACK_USER_TOKEN")
    if not token:
        return "Error: SLACK_USER_TOKEN not configured"
    sandbox.commands.run("mkdir -p ~/attachments")
    url = "https://slack.com/api/files.list"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"channel": channel_id, "ts_from": "0", "ts_to": str(int(float(thread_ts) + 1)), "count": limit}
    try:
        response = requests.get(url, headers=headers, params=params)
        res_json = response.json()
        if not res_json.get("ok"):
            return f"Slack API error: {res_json.get('error', 'unknown')}"
        files = res_json.get("files", [])
        if not files:
            return "No files found in this thread."
        results = []
        for f in files:
            file_url = f.get("url_private_download") or f.get("url_private")
            if not file_url:
                continue
            file_resp = requests.get(file_url, headers={"Authorization": f"Bearer {token}"})
            if file_resp.status_code != 200:
                results.append(f"✗ {f.get('name')}: failed to download")
                continue
            filename = f.get("name", "unknown")
            sandbox.files.write(f"/home/user/attachments/{filename}", file_resp.content)
            results.append(f"✓ {filename} ({len(file_resp.content)} bytes)")
        return "Downloaded to ~/attachments/:\n" + "\n".join(results)
    except Exception as e:
        return f"Error downloading attachments: {str(e)}"


@agent.tool
def download_attachments_to_sandbox(ctx: RunContext[AgentDeps]) -> str:
    """Download Slack file attachments from the current thread to sandbox's ~/attachments/."""
    channel_id = ctx.deps.channel_id
    thread_ts = ctx.deps.thread_ts
    user_token = ctx.deps.user_token or os.environ.get("SLACK_USER_TOKEN")
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured"
    try:
        sandbox_id = get_thread_sandbox_id(channel_id, thread_ts)
        if not sandbox_id:
            return "No active sandbox. Run a command first."
        sandbox = Sandbox.connect(sandbox_id)
        return download_slack_attachments(channel_id, thread_ts, sandbox, user_token)
    except Exception as e:
        return f"Error: {str(e)}"


@agent.tool
def upload_file_from_sandbox(
    ctx: RunContext[AgentDeps], filepath: str, title: str = "", initial_comment: str = "",
) -> str:
    """Upload a file from the sandbox to the current Slack channel/thread."""
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
        file_content = sandbox.files.read(filepath)
        if file_content is None:
            return f"Error: File not found at {filepath}"
        filename = os.path.basename(filepath)
        url = "https://slack.com/api/files.upload"
        headers = {"Authorization": f"Bearer {user_token}"}
        files = {"file": (filename, file_content)}
        data = {"channels": channel_id, "title": title or filename, "initial_comment": initial_comment}
        if thread_ts:
            data["thread_ts"] = thread_ts
        response = requests.post(url, headers=headers, files=files, data=data)
        res_json = response.json()
        if res_json.get("ok"):
            return f"Uploaded {filename} to channel {channel_id}"
        return f"Slack upload error: {res_json.get('error', 'unknown')}"
    except Exception as e:
        return f"Error uploading file: {str(e)}"


@agent.tool
def search_web_tool(ctx: RunContext[AgentDeps], query: str, num_results: int = 8) -> str:
    """Search the web using Exa. Returns results with titles, URLs, and snippets.
    
    Use for: current events, research, finding resources, verifying facts.
    
    Args:
        query: The search query string.
        num_results: Number of results (1-20, default 8).
    """
    from agent.tools.web_search import search_web
    return search_web(query, num_results)


@agent.tool
def analyze_image_tool(ctx: RunContext[AgentDeps], image_path: str, prompt: str = "Describe this image in detail.") -> str:
    """Analyze an image using AI vision capabilities.
    
    Use this when users share images and ask what's in them, want text extracted,
    objects identified, etc. First download the image with download_attachments_to_sandbox,
    then read it and pass the data here.
    
    Args:
        image_path: Path to the image file in the sandbox (e.g., ~/attachments/photo.jpg).
        prompt: What to look for / analyze (default: describe the image).
    """
    channel_id = ctx.deps.channel_id
    thread_ts = ctx.deps.thread_ts
    sandbox_id = get_thread_sandbox_id(channel_id, thread_ts)
    if not sandbox_id:
        return "No active sandbox. Use download_attachments_to_sandbox first."
    try:
        sandbox = Sandbox.connect(sandbox_id)
        image_data = sandbox.files.read(image_path)
        if image_data is None:
            return f"Error: File not found at {image_path}"
        from agent.tools.vision import analyze_image
        filename = os.path.basename(image_path)
        return analyze_image(image_data, filename, prompt)
    except Exception as e:
        return f"Error analyzing image: {str(e)}"


@agent.tool
def generate_image_tool(ctx: RunContext[AgentDeps], prompt: str, n: int = 1, size: str = "1024x1024") -> str:
    """Generate AI images from a text prompt using DALL-E 3.
    
    Requires the user to have an OpenAI API key (via BYOK or global OPENAI_API_KEY).
    
    Args:
        prompt: Text description of the desired image.
        n: Number of images (1-4, default 1).
        size: Size ("1024x1024", "1792x1024", "1024x1792", default "1024x1024").
    """
    from agent.tools.image_gen import generate_image
    return generate_image(prompt, n, size)


@agent.tool
def render_mermaid_tool(ctx: RunContext[AgentDeps], diagram_code: str, theme: str = "default") -> str:
    """Render a Mermaid diagram and return a URL to the PNG image.
    
    Supports: flowcharts, sequence diagrams, class diagrams, state diagrams,
    Gantt charts, pie charts, entity relationship diagrams, user journey, etc.
    The URL can be embedded via send_web_embed_tool or downloaded.
    
    Args:
        diagram_code: Mermaid diagram definition (e.g., "graph TD; A-->B;").
        theme: Theme ("default", "dark", "forest", "neutral", default "default").
    """
    from agent.tools.mermaid_tool import render_mermaid
    url = render_mermaid(diagram_code, theme)
    if url.startswith("http"):
        return f"Diagram rendered: {url}"
    return url


@agent.tool
def summarize_thread_tool(ctx: RunContext[AgentDeps], channel_id: str = "", thread_ts: str = "") -> str:
    """Summarize a Slack thread by fetching its messages and condensing them.
    
    If channel_id and thread_ts are empty, summarizes the current conversation.
    
    Args:
        channel_id: Channel ID (default: current channel).
        thread_ts: Thread timestamp (default: current thread).
    """
    if not channel_id:
        channel_id = ctx.deps.channel_id
    if not thread_ts:
        thread_ts = ctx.deps.thread_ts
    user_token = ctx.deps.user_token or os.environ.get("SLACK_USER_TOKEN")
    from agent.tools.summarize_thread import summarize_thread
    return summarize_thread(channel_id, thread_ts, user_token)


@agent.tool
def list_channel_threads_tool(ctx: RunContext[AgentDeps], channel_id: str = "", limit: int = 10) -> str:
    """List recent threads in a Slack channel.
    
    Shows thread starters with reply counts and timestamps.
    
    Args:
        channel_id: Channel ID (default: current channel).
        limit: Max threads to return (default 10).
    """
    if not channel_id:
        channel_id = ctx.deps.channel_id
    user_token = ctx.deps.user_token or os.environ.get("SLACK_USER_TOKEN")
    from agent.tools.list_threads import list_channel_threads
    return list_channel_threads(channel_id, limit, user_token)


@agent.tool
def schedule_reminder_tool(ctx: RunContext[AgentDeps], text: str, delay_seconds: int) -> str:
    """Schedule a one-time reminder that will be DM'd to you.
    
    Args:
        text: Reminder message text.
        delay_seconds: Seconds from now until reminder fires (max ~120 days).
    """
    from agent.tools.reminder_tool import schedule_reminder_tool as srt
    return srt(ctx.deps.user_id, ctx.deps.channel_id, text, delay_seconds)


@agent.tool
def read_sandbox_file_tool(ctx: RunContext[AgentDeps], path: str) -> str:
    """Read a file from the sandbox filesystem.
    
    Args:
        path: Path to file (e.g., /home/user/file.txt or ~/attachments/data.csv).
    """
    from agent.tools.sandbox_files import read_sandbox_file
    return read_sandbox_file(ctx.deps.channel_id, ctx.deps.thread_ts, path)


@agent.tool
def write_sandbox_file_tool(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
    """Write content to a file in the sandbox filesystem. Creates parent dirs.
    
    Args:
        path: Path to write (e.g., /home/user/output.txt).
        content: Text content to write.
    """
    from agent.tools.sandbox_files import write_sandbox_file
    return write_sandbox_file(ctx.deps.channel_id, ctx.deps.thread_ts, path, content)


@agent.tool
def search_sandbox_files_tool(ctx: RunContext[AgentDeps], pattern: str, path: str = "/home/user") -> str:
    """Search for text patterns in sandbox files (grep).
    
    Args:
        pattern: Regex or text pattern to search for.
        path: Directory to search (default: /home/user).
    """
    from agent.tools.sandbox_files import search_sandbox_files
    return search_sandbox_files(ctx.deps.channel_id, ctx.deps.thread_ts, pattern, path)


@agent.tool
def list_sandbox_files_tool(ctx: RunContext[AgentDeps], pattern: str = "*", path: str = "/home/user") -> str:
    """List files in the sandbox matching a glob pattern.
    
    Args:
        pattern: Glob pattern (default: "*").
        path: Directory to search (default: /home/user).
    """
    from agent.tools.sandbox_files import list_sandbox_files
    return list_sandbox_files(ctx.deps.channel_id, ctx.deps.thread_ts, pattern, path)


@agent.tool
def extract_tar_gz_tool(ctx: RunContext[AgentDeps], archive_path: str, extract_to: str = "/home/user/data") -> str:
    """Extract a .tar.gz or .tgz file in the sandbox.
    
    Use this for large archives (e.g., 500MB+ of CSV files).
    Files will be available at the extract_to path for further analysis.
    
    Args:
        archive_path: Path to the .tar.gz file in sandbox (e.g., ~/attachments/data.tar.gz).
        extract_to: Directory to extract to (default: /home/user/data).
    """
    from agent.tools.data_analysis import extract_tar_gz_in_sandbox
    return extract_tar_gz_in_sandbox(ctx.deps.channel_id, ctx.deps.thread_ts, archive_path, extract_to)


@agent.tool
def analyze_csv_tool(ctx: RunContext[AgentDeps], csv_path: str, query: str = "") -> str:
    """Analyze a CSV file in the sandbox using pandas.
    
   
    Args:
        csv_path: Path to the CSV file in sandbox.
        query: Optional analysis question or pandas code to run (e.g., "df.groupby('col').sum()").
    """
    from agent.tools.data_analysis import analyze_csv_in_sandbox
    return analyze_csv_in_sandbox(ctx.deps.channel_id, ctx.deps.thread_ts, csv_path, query)


@agent.tool
def run_sql_on_csv_tool(ctx: RunContext[AgentDeps], csv_path: str, sql_query: str) -> str:
    """Run SQL queries on CSV files using DuckDB in the sandbox.
    
    The CSV is loaded as a table named 'data'.
    
    Args:
        csv_path: Path to the CSV file in sandbox.
        sql_query: SQL query to run (table name is 'data').
    """
    from agent.tools.data_analysis import run_sql_on_csv
    return run_sql_on_csv(ctx.deps.channel_id, ctx.deps.thread_ts, csv_path, sql_query)


@agent.tool
def run_python_data_analysis_tool(ctx: RunContext[AgentDeps], code: str) -> str:
    """Run arbitrary Python data analysis code in the sandbox with pandas/numpy/duckdb pre-loaded.
    
    Has access to: pd (pandas), np (numpy), duckdb, conn (DuckDB connection).
    
    Args:
        code: Python code to execute.
    """
    from agent.tools.data_analysis import run_python_data_analysis
    return run_python_data_analysis(ctx.deps.channel_id, ctx.deps.thread_ts, code)


@agent.tool
def install_opencode_tool(ctx: RunContext[AgentDeps]) -> str:
    """Install opencode (open-source AI coding agent) in the sandbox.
    
    Opencode is like Claude Code but open-source. Use it for complex coding tasks.
    Run this once per sandbox session, then use run_opencode_tool.
    
    Returns:
        Installation status.
    """
    from agent.tools.data_analysis import install_opencode_in_sandbox
    return install_opencode_in_sandbox(ctx.deps.channel_id, ctx.deps.thread_ts)


@agent.tool
def run_opencode_tool(ctx: RunContext[AgentDeps], task: str, model: str = "") -> str:
    """Run opencode in the sandbox to perform complex coding tasks.
    
    Opencode is an open-source AI coding agent (like Claude Code).
    It can read/write files, run commands, and use tools to complete tasks.
    Install it first with install_opencode_tool.
    
    Args:
        task: The task/question for opencode to complete.
        model: Optional model override (e.g., "anthropic/claude-sonnet-4-6").
    """
    from agent.tools.data_analysis import run_opencode_in_sandbox
    return run_opencode_in_sandbox(ctx.deps.channel_id, ctx.deps.thread_ts, task, model)


def send_web_embed(
    channel_id: str, text: str, url: str, title: str,
    thumbnail_url: str = "https://placehold.co/1280x720?text=click%20to%20open%20the%20\\ncoolton%20embed",
    user_token: str | None = None,
) -> str:
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return "Error: SLACK_BOT_TOKEN not configured"
    blocks = [{
        "type": "video", "video_url": url, "title_url": url,
        "thumbnail_url": thumbnail_url,
        "title": {"type": "plain_text", "text": title},
        "alt_text": title,
    }]
    payload = {"channel": channel_id, "text": text, "blocks": blocks}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    try:
        response = requests.post("https://slack.com/api/chat.postMessage", json=payload, headers=headers)
        res_json = response.json()
        if res_json.get("ok"):
            return f"Success: Embed sent to {channel_id}"
        error = res_json.get("error", "unknown")
        metadata = res_json.get("response_metadata", {})
        return f"Error: {error} | url: {url} | metadata: {metadata}"
    except Exception as e:
        return f"Error sending web embed: {str(e)}"


def send_whiteboard_embed(
    channel_id: str, text: str = "whiteboard", title: str = "whiteboard",
    whiteboard_id: int | None = None, user_token: str | None = None,
) -> str:
    if whiteboard_id is None:
        whiteboard_id = random.randint(100000, 999999)
    url = f"https://whiteboard.felix.hackclub.app/{whiteboard_id}"
    thumbnail_url = "https://placehold.co/1280x720?text=click%20to%20open%20the\\ncoolton%20embed"
    text_with_id = f"{text} #{whiteboard_id}"
    title_with_id = f"{title} #{whiteboard_id}"
    result = send_web_embed(channel_id=channel_id, text=text_with_id, url=url, title=title_with_id, thumbnail_url=thumbnail_url)
    if result.startswith("Success"):
        return f"{result} (whiteboard id: {whiteboard_id})"
    return result


@agent.tool
def send_whiteboard_embed_tool(
    ctx: RunContext[AgentDeps], text: str = "whiteboard",
    title: str = "whiteboard", whiteboard_id: int | None = None,
) -> str:
    """Send a Felix whiteboard (tldraw) embed to the current channel.
    
    Creates a new whiteboard with a random ID at felix's tldraw instance.
    
    Args:
        text: Fallback text (default: "whiteboard").
        title: Embed title (default: "whiteboard").
        whiteboard_id: Optional specific ID (default: random).
    """
    return send_whiteboard_embed(channel_id=ctx.deps.channel_id, text=text, title=title, whiteboard_id=whiteboard_id)


def minify_html(html: str) -> str:
    html = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
    html = re.sub(r"\s+", " ", html)
    html = re.sub(r"\s*>\s*<", "><", html)
    return html.strip()


def send_html_embed(
    channel_id: str, html: str, text: str = "html embed", title: str = "html embed",
    thumbnail_url: str = "https://placehold.co/1280x720?text=click%20to%20open%20the\\ncoolton%20embed",
    user_token: str | None = None,
) -> str:
    minified = minify_html(html)
    b64 = base64.urlsafe_b64encode(minified.encode()).decode().rstrip("=")
    url = f"https://tanjim.org:2390/{b64}"
    return send_web_embed(channel_id=channel_id, text=text, url=url, title=title, thumbnail_url=thumbnail_url, user_token=user_token)


@agent.tool
def send_html_embed_tool(
    ctx: RunContext[AgentDeps], html: str, text: str = "html embed",
    title: str = "html embed",
    thumbnail_url: str = "https://placehold.co/1280x720?text=click%20to%20open%20the\\ncoolton%20embed",
) -> str:
    """Send custom HTML as a live embed in the current channel.
    
    Your HTML is minified, base64-encoded, and served via https://tanjim.org:2390.
    
    Args:
        html: Raw HTML content.
        text: Fallback text (default: "html embed").
        title: Embed title (default: "html embed").
        thumbnail_url: Optional custom thumbnail.
    """
    return send_html_embed(channel_id=ctx.deps.channel_id, html=html, text=text, title=title, thumbnail_url=thumbnail_url)


@agent.tool
def slack_api_call(ctx: RunContext[AgentDeps], method: str, params: dict) -> str:
    """Make an arbitrary Slack API call as cooltonUser.
    
    Use for any Slack Web API method not covered by other tools.
    
    Args:
        method: Slack API method (e.g., 'chat.postMessage', 'conversations.list').
        params: Dictionary of parameters for the method.
    """
    user_token = os.environ.get("SLACK_USER_TOKEN")
    if not user_token:
        return "Error: SLACK_USER_TOKEN not configured"
    url = f"https://slack.com/api/{method}"
    headers = {"Authorization": f"Bearer {user_token}", "Content-Type": "application/json; charset=utf-8"}
    try:
        response = requests.post(url, json=params, headers=headers)
        res_json = response.json()
        if res_json.get("ok"):
            return f"Success: {res_json}"
        return f"Slack API error: {res_json.get('error', 'unknown')}"
    except Exception as e:
        return f"Error: {str(e)}"


@agent.tool
def slack_api_call_as_bot_tool(ctx: RunContext[AgentDeps], method: str, params: dict) -> str:
    """Make an arbitrary Slack API call as the BOT (not cooltonUser).
    
    Uses SLACK_BOT_TOKEN. Use for bot-level actions like posting messages as the bot,
    updating bot messages, managing bot's own reactions, etc.
    
    Args:
        method: Slack API method (e.g., 'chat.postMessage', 'chat.update', 'reactions.add').
        params: Dictionary of parameters for the method.
    """
    from agent.tools.slack_bot_api import slack_api_call_as_bot
    return slack_api_call_as_bot(method, params)


@agent.tool
def leave_thread_tool(ctx: RunContext[AgentDeps]) -> str:
    """Leave the current thread - bot will ignore all future messages in this thread until @mentioned again.
    
    Use this when you want to stop responding in a thread but still want to be available if mentioned.
    """
    from agent.leave_thread_store import leave_thread
    return leave_thread(ctx.deps.channel_id, ctx.deps.thread_ts)


@agent.tool
def send_message(ctx: RunContext[AgentDeps], text: str) -> str:
    """Send a message to the current Slack thread mid-turn. Use this to post progress updates,
    intermediate results, or messages that don't wait for the final response.
    
    Args:
        text: The message content to send (Markdown supported).
    """
    try:
        ctx.deps.client.chat_postMessage(
            channel=ctx.deps.channel_id,
            thread_ts=ctx.deps.thread_ts,
            text=text,
        )
        return "Message sent."
    except Exception as e:
        return f"Failed to send message: {e}"


@agent.tool
def skip(ctx: RunContext[AgentDeps]) -> str:
    """Skip sending the final response message at the end of your turn.
    
    Use this when the user's request doesn't need a reply, when you've already
    responded via send_message, or when you have nothing to add.
    """
    ctx.deps.should_skip = True
    return "Final message will be skipped."


@agent.tool
def install_skill(ctx: RunContext[AgentDeps], package: str, skill: str = "") -> str:
    """Install a new agent skill from the skills.sh marketplace (Vercel's Agent Skills CLI).

    Run this when the user asks to "install a skill", "add a skill", or names a
    skill package/repo they want (e.g. `vercel-labs/agent-skills`, or a GitHub URL).
    After install, the skill is available immediately via load_skill / list_skills.

    Args:
        package: The skill package to install. Either `owner/repo` (e.g.
            `vercel-labs/agent-skills`) or a full GitHub URL
            (e.g. `https://github.com/vercel-labs/agent-skills`).
        skill: Optional specific skill name inside a multi-skill repo. Leave empty
            to install all skills in the package.
    """
    import shutil
    import subprocess

    cmd = ["npx", "-y", "skills@latest", "add", package, "-y"]
    if skill:
        cmd += ["-s", skill]
    try:
        proc = subprocess.run(
            cmd,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return "Error: skill install timed out after 180s."
    except FileNotFoundError:
        return "Error: npx/node not found on this system."

    if proc.returncode != 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        return f"Failed to install skill (exit {proc.returncode}):\n{out[-1500:]}"

    # The CLI installs into .agents/skills/<name>; make sure it's picked up.
    out = proc.stdout or ""
    return f"Skill install complete.\n{out[-1200:]}"


@agent.tool
def agentmail_create_inbox(ctx: RunContext[AgentDeps]) -> str:
    """Create a new AgentMail inbox for coolton (gives coolton its own @agentmail.to address).

    Use when you need a fresh email identity to send/receive mail autonomously.
    """
    from agent.tools.agentmail import create_inbox_tool

    return create_inbox_tool()


@agent.tool
def agentmail_list_inboxes(ctx: RunContext[AgentDeps], limit: int = 20) -> str:
    """List coolton's AgentMail inboxes (ids + @agentmail.to addresses)."""
    from agent.tools.agentmail import list_inboxes_tool

    return list_inboxes_tool(limit=limit)


@agent.tool
def agentmail_list_messages(ctx: RunContext[AgentDeps], inbox_id: str = "coolton@agentmail.to", limit: int = 20) -> str:
    """List recent messages in a coolton AgentMail inbox.

    Args:
        inbox_id: The inbox id or @agentmail.to address (defaults to coolton@agentmail.to).
        limit: Max messages to return (default 20).
    """
    from agent.tools.agentmail import list_messages_tool

    return list_messages_tool(inbox_id, limit=limit)


@agent.tool
def agentmail_read_message(ctx: RunContext[AgentDeps], message_id: str, inbox_id: str = "coolton@agentmail.to") -> str:
    """Read the full content of a specific AgentMail message.

    Args:
        message_id: The message id from agentmail_list_messages.
        inbox_id: The inbox id or @agentmail.to address (defaults to coolton@agentmail.to).
    """
    from agent.tools.agentmail import read_message_tool

    return read_message_tool(inbox_id, message_id)


@agent.tool
def agentmail_send_email(
    ctx: RunContext[AgentDeps],
    to: str,
    subject: str,
    text: str,
    inbox_id: str = "coolton@agentmail.to",
    cc: str = "",
    html: str = "",
) -> str:
    """Send an email from a coolton AgentMail inbox.

    Args:
        to: Recipient email address (or comma-separated list).
        subject: Email subject.
        text: Plain-text body.
        inbox_id: The inbox id or @agentmail.to address to send from (defaults to coolton@agentmail.to).
        cc: Optional CC address(es), comma-separated.
        html: Optional HTML body (used only if text is empty).
    """
    from agent.tools.agentmail import send_email_tool

    return send_email_tool(to, subject, text, inbox_id=inbox_id, cc=cc, html=html)


def _repo_root() -> str:
    return os.path.abspath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _skill_dirs() -> list[str]:
    root = _repo_root()
    return [os.path.join(root, "skills"), os.path.join(root, ".agents", "skills")]


def _is_within(path: str, parent: str) -> bool:
    """True only if `path` is the same as or nested under `parent` (no traversal)."""
    path = os.path.abspath(path)
    parent = os.path.abspath(parent)
    return path == parent or path.startswith(parent + os.sep)


def _build_skill_md(slug: str, description: str, body: str) -> str:
    """Build a SKILL.md string with valid YAML frontmatter.

    The description is single-quoted so embedded colons (the exact thing that
    broke the catalog before) can't terminate the YAML mapping early.
    """
    desc = description.replace("'", "\\'")
    return (
        "---\n"
        f"name: {slug}\n"
        f"description: '{desc}'\n"
        "---\n\n"
        f"# {slug.replace('-', ' ').title()}\n\n"
        f"{body}\n"
    )


def _validate_skill_md(content: str) -> tuple[bool, str]:
    """Return (ok, error) for a SKILL.md's frontmatter.

    Parses the leading YAML block so a malformed skill is caught before it can
    enter the catalog and break every model's skill scan.
    """
    try:
        import yaml
    except ImportError:
        return True, ""  # yaml unavailable — skip validation rather than block
    if not content.startswith("---"):
        return False, "missing frontmatter delimiters"
    end = content.find("\n---", 3)
    if end == -1:
        return False, "unterminated frontmatter"
    block = content[3:end].strip()
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as e:
        return False, f"invalid YAML: {e}"
    if not isinstance(data, dict):
        return False, "frontmatter is not a mapping"
    if not data.get("name") or not data.get("description"):
        return False, "name and description are required"
    return True, ""


def _resolve_skill(name: str) -> str | None:
    """Find the on-disk folder for a skill by name across known skill dirs.

    Only direct children of a known skill dir are matched; names containing path
    separators or traversal sequences are rejected (returns None).
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    for base in _skill_dirs():
        cand = os.path.join(base, name)
        # cand must be a direct child of a known skill dir
        if os.path.dirname(os.path.abspath(cand)) != os.path.abspath(base):
            continue
        if os.path.isdir(cand) and os.path.exists(os.path.join(cand, "SKILL.md")):
            return cand
    return None


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", name.strip().lower())


@agent.tool
def create_skill(ctx: RunContext[AgentDeps], name: str, description: str, body: str = "") -> str:
    """Create a new custom agent skill in the repo's `skills/` directory.

    Use this when the user wants to "make a skill", "create a skill for X",
    "turn this workflow into a skill", or save a reusable playbook. This writes
    a proper SKILL.md (frontmatter + instructions) so the skill is immediately
    discoverable via list_skills / load_skill. Do NOT use shell/CLI commands in
    the sandbox to create skills — they have no effect on the agent.

    Args:
        name: Skill name (will be slugified, e.g. "My Cool Skill" -> "my-cool-skill").
        description: One-line description; used for skill discovery. Describe when
            the skill should trigger.
        body: The skill's instructions/body (Markdown). If empty, a minimal
            template is created for you to fill in later.
    """
    slug = _safe_name(name)
    if not slug:
        return "Error: invalid skill name."
    target = os.path.join(_repo_root(), "skills", slug)
    if not _is_within(target, os.path.join(_repo_root(), "skills")):
        return "Error: invalid skill name (must not escape the skills directory)."
    if os.path.exists(target):
        return f"Error: a skill named '{slug}' already exists at {target}."
    try:
        os.makedirs(target, exist_ok=True)
    except OSError as e:
        return f"Error creating skill directory: {e}"
    if not body.strip():
        body = (
            "# " + slug.replace("-", " ").title() + "\n\n"
            "Describe the workflow, steps, and guidance for this skill here.\n"
        )
    content = _build_skill_md(slug, description.strip(), body.strip())
    # Validate before writing: a malformed SKILL.md would break the whole skill
    # catalog load (every model that scans skills chokes on bad frontmatter).
    # If invalid, reject and do NOT create the skill.
    ok, err = _validate_skill_md(content)
    if not ok:
        return (
            f"Error: generated SKILL.md failed validation ({err}). The skill was "
            "NOT created. Fix the description/body (avoid unquoted colons in the "
            "description) and try again."
        )
    try:
        with open(os.path.join(target, "SKILL.md"), "w") as f:
            f.write(content)
    except OSError as e:
        return f"Error writing SKILL.md: {e}"
    return (
        f"Created skill '{slug}' at skills/{slug}/SKILL.md. "
        "It is now available via list_skills / load_skill."
    )


@agent.tool
def rename_skill(ctx: RunContext[AgentDeps], old_name: str, new_name: str) -> str:
    """Rename an existing agent skill (moves its folder and updates frontmatter name).

    Use this when the user wants to rename a skill. Operates on skills found in
    the repo's `skills/` or `.agents/skills/` directories. Do NOT use sandbox
    shell commands — they have no effect on the agent.

    Args:
        old_name: Current skill name/folder.
        new_name: Desired new skill name (will be slugified).
    """
    src = _resolve_skill(old_name)
    if not src:
        return f"Error: skill '{old_name}' not found in any skill directory."
    new_slug = _safe_name(new_name)
    if not new_slug:
        return "Error: invalid new skill name."
    dst = os.path.join(os.path.dirname(src), new_slug)
    if os.path.exists(dst):
        return f"Error: a skill named '{new_slug}' already exists."
    try:
        os.rename(src, dst)
        sk_md = os.path.join(dst, "SKILL.md")
        if os.path.exists(sk_md):
            with open(sk_md, "r") as f:
                txt = f.read()
            txt = re.sub(r"(?m)^name:\s*.*$", f"name: {new_slug}", txt, count=1)
            with open(sk_md, "w") as f:
                f.write(txt)
    except OSError as e:
        return f"Error renaming skill: {e}"
    return f"Renamed skill '{old_name}' -> '{new_slug}'."


@agent.tool
def delete_skill(ctx: RunContext[AgentDeps], name: str) -> str:
    """Delete an agent skill folder entirely from disk.

    Use this when the user wants to remove/uninstall a skill. This is permanent.
    Operates on skills in the repo's `skills/` or `.agents/skills/` directories.
    Do NOT use sandbox shell commands — they have no effect on the agent.

    Args:
        name: Skill name/folder to delete.
    """
    src = _resolve_skill(name)
    if not src:
        return f"Error: skill '{name}' not found in any skill directory."
    try:
        shutil.rmtree(src)
    except OSError as e:
        return f"Error deleting skill: {e}"
    return f"Deleted skill '{name}' from {src}."


def _resolve_display_name(client, user_id: str) -> str:
    """Best-effort Slack display-name lookup for a user id (falls back to the id)."""
    if not user_id or not client:
        return user_id or "unknown"
    try:
        resp = client.users_info(user=user_id)
        if resp.get("ok"):
            profile = resp["user"].get("profile", {})
            return profile.get("display_name") or profile.get("real_name") or resp["user"].get("name") or user_id
    except Exception:
        pass
    return user_id


def _tag_user_message(text: str, deps) -> str:
    """Prefix a user turn with `USER_ID (DisplayName):` so the model knows who said it."""
    uid = deps.user_id or "unknown"
    name = _resolve_display_name(deps.client, uid)
    return f"{uid} ({name}):\n{text}"


def run_agent(text, deps, message_history=None):
    # Provider fallback order: BYOK endpoint → Anthropic → OpenAI → OpenRouter → Cerebras
    provider_order = _build_provider_order(deps.user_id)

    # Attribute the incoming message to its sender so the model can tell users apart.
    text = _tag_user_message(text, deps)
    
    if not provider_order:
        raise RuntimeError("No AI provider configured.")

    from agent.fallback_cache import get_working_provider, set_working_provider, clear_cache as _clear_cache

    cached_provider = get_working_provider(deps.user_id)
    if cached_provider:
        for i, (name, _) in enumerate(provider_order):
            if name == cached_provider:
                provider_order.insert(0, provider_order.pop(i))
                logger.info(f"Fallback cache: trying {cached_provider} first for {deps.user_id}")
                break

    # Retry configuration
    max_retries = 3
    base_delay = 2.0
    retryable_errors = [
        "ResourceExhausted",
        "RateLimitError",
        "rate_limit",
        "quota",
        "429",
        "503",
        "504",
        "timeout",
        "connection",
    ]

    def is_retryable_error(error: Exception) -> bool:
        error_str = str(error).lower()
        return any(retryable in error_str.lower() for retryable in retryable_errors)

    def is_fatal_error(error: Exception) -> bool:
        error_str = str(error).lower()
        fatal_patterns = [
            "coroutine",
            "has no len()",
            "has no attribute",
            "'module' object is not callable",
        ]
        return any(p in error_str for p in fatal_patterns)

    all_errors = []
    
    for provider_name, provider_config in provider_order:
        for attempt in range(max_retries):
            try:
                model_name = provider_config["model"]
                
                # Create model object if custom base_url (BYOK, HCAI)
                model_obj = None
                if provider_config.get("base_url"):
                    from pydantic_ai.models.openai import OpenAIChatModel
                    from pydantic_ai.providers.openai import OpenAIProvider
                    model_obj = OpenAIChatModel(
                        provider_config["model"],
                        provider=OpenAIProvider(
                            base_url=provider_config["base_url"],
                            api_key=provider_config["api_key"],
                        ),
                    )
                
                # Set env vars for this provider
                if provider_name not in ("byok", "hcai", "hcai_minimax", "hcai_hy3_free", "hcai_hy3") and provider_config.get("api_key"):
                    if provider_name == "anthropic":
                        os.environ["ANTHROPIC_API_KEY"] = provider_config["api_key"]
                    elif provider_name == "openai":
                        os.environ["OPENAI_API_KEY"] = provider_config["api_key"]
                    elif provider_name in ("jams", "openrouter_fb", "jams_hy3_free", "jams_hy3", "openrouter_hy3_free"):
                        os.environ["OPENROUTER_API_KEY"] = provider_config["api_key"]
                    elif provider_name in ("gemini", "gemini_gemma"):
                        os.environ["GOOGLE_API_KEY"] = provider_config["api_key"]
                    elif provider_name == "mistral":
                        os.environ["MISTRAL_API_KEY"] = provider_config["api_key"]
                    elif provider_name.startswith("groq_"):
                        os.environ["GROQ_API_KEY"] = provider_config["api_key"]
                    elif provider_name == "cerebras":
                        os.environ["CEREBRAS_API_KEY"] = provider_config["api_key"]
                
                # Rate limit for Cerebras
                if "cerebras" in model_name.lower():
                    enforce_rate_limit()

                # Build dynamic prompt
                from listeners.actions.instructions_actions import get_user_instructions as _get_instructions
                custom_instructions = _get_instructions(deps.user_id)
                deps.custom_instructions = custom_instructions

                context_info = f"""
## CURRENT CONTEXT
- You are in channel_id: `{deps.channel_id}` (thread_ts: `{deps.thread_ts}` if in thread, else DM)
- Use this channel_id for operations in the current channel unless user specifies otherwise
- Your user_id (the HUMAN who messaged you): `{deps.user_id}`
- Your own bot user id (this is YOU, not a third party): `{os.environ.get("COOLTON_BOT_ID", "")}`
- Your cooltonUser helper account id (acts on your behalf): `{os.environ.get("COOLTON_USER_ID", "")}`
- Message timestamp: `{deps.message_ts}`
"""
                full_prompt = SYSTEM_PROMPT + context_info
                if custom_instructions:
                    full_prompt += f"\n\n## USER'S CUSTOM INSTRUCTIONS\n{custom_instructions}\n"

                toolsets = []
                deps.user_token = deps.user_token or os.environ.get("SLACK_USER_TOKEN")
                if deps.user_token:
                    logger.info("Slack MCP Server enabled (user_token present)")
                    try:
                        transport = StreamableHttpTransport(
                            SLACK_MCP_URL,
                            headers={"Authorization": f"Bearer {deps.user_token}"},
                        )
                        toolsets.append(MCPToolset(transport))
                    except Exception as e:
                        logger.exception(f"Failed to create MCP server: {e}")
                else:
                    logger.info("Slack MCP Server disabled (no user_token)")

                all_tools = list(agent._function_toolset.tools.values())
                tool_functions = [t.function for t in all_tools]

                agent_dynamic = Agent(
                    deps_type=AgentDeps,
                    system_prompt=full_prompt,
                    tools=tool_functions,
                )

                capabilities = [PrepareTools(disable_strict_for_all_tools)]
                if deps.plan_ts:
                    from agent.plan_block import build_plan_hooks
                    capabilities.append(build_plan_hooks())

                from pydantic_ai_skills import SkillsCapability
                capabilities.append(
                    SkillsCapability(
                        directories=["skills", ".agents/skills"],
                        auto_reload=True,
                    )
                )

                run_kwargs = dict(
                    user_prompt=text,
                    deps=deps,
                    message_history=message_history,
                    toolsets=toolsets,
                    capabilities=capabilities,
                )
                if model_obj:
                    run_kwargs["model"] = model_obj
                else:
                    run_kwargs["model"] = model_name

                result = agent_dynamic.run_sync(**run_kwargs)
                set_working_provider(deps.user_id, provider_name)
                deps.model_used = f"{provider_name} / {model_name}"
                return result

            except Exception as e:
                if is_fatal_error(e):
                    logger.critical(f"Fatal error in {provider_name}: {e}")
                    raise
                all_errors.append(f"{provider_name}: {e}")
                if is_retryable_error(e) and attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"{provider_name} attempt {attempt + 1} failed with retryable error: {e}. Retrying in {delay}s...")
                    time.sleep(delay)
                    continue
                else:
                    logger.warning(f"{provider_name} failed (attempt {attempt + 1}/{max_retries}): {e}")
                    break  # Try next provider
        
        # If the cached provider failed, clear the cache so next call starts fresh
        if cached_provider and provider_name == cached_provider:
            _clear_cache(deps.user_id)
            logger.warning(f"Cached provider {cached_provider} failed, cleared cache")

        # All retries exhausted for this provider, try next provider
        logger.warning(f"Provider {provider_name} exhausted all retries, trying next provider...")
    
    # All providers failed
    errors_str = "\n".join(f"  - {err}" for err in all_errors)
    raise RuntimeError(f"All AI providers failed.\n{errors_str}")


def disable_strict_for_all_tools(ctx, tool_defs):
    return [replace(tool_def, strict=False) for tool_def in tool_defs]
