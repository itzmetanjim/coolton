import logging
import os
import random
import re
import time
import json
import shutil
import subprocess
import threading
import requests
from pydantic_ai import RunContext
from pydantic_ai import Agent
from pydantic_ai.messages import BinaryContent, ToolReturn
from pydantic_ai.capabilities import Hooks, PrepareTools
from dataclasses import replace
from agent.deps import AgentDeps
from agent.platforms.slack import SlackPlatform
from agent.stop_store import HaltRun
from agent.tools import add_emoji_reaction
from agent.byok_store import get_text_endpoint_id, get_endpoint_decrypted
from agent.redact import redact as _redact, strip_secret_keys as _strip_secret_keys
from e2b import Sandbox
from agent.sandbox_store import get_thread_sandbox_id
from agent.sandbox_helpers import get_or_create_sandbox, _proxy_env
from agent.github_proxy_client import PUBLIC_PROXY_HOST
from agent.tool_proxy import (
    build_sandbox_module,
    format_signatures,
    register_sandbox,
    start as start_tool_proxy,
)

logger = logging.getLogger(__name__)

rate_limit_lock = threading.Lock()
_last_request_time = 0.0
RATE_LIMIT_INTERVAL = 15.0

def enforce_rate_limit():
    global _last_request_time
    now = time.time()
    with rate_limit_lock:
        elapsed = now - _last_request_time
        if elapsed >= RATE_LIMIT_INTERVAL:
            _last_request_time = now
            return
        sleep_needed = RATE_LIMIT_INTERVAL - elapsed
        _last_request_time = now + sleep_needed
    logger.warning(f"Rate Limit Check: Sleeping for {sleep_needed:.2f}s")
    time.sleep(sleep_needed)

SYSTEM_PROMPT = SlackPlatform().system_prompt

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
    elif os.environ.get("OPENROUTER_API_KEY"):
        _cached_model = "openrouter:openai/gpt-4.1-mini"
    elif os.environ.get("OPENROUTER_API_KEY_FALLBACK"):
        _cached_model = "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free"
    elif os.environ.get("GOOGLE_API_KEY"):
        _cached_model = "google:gemini-3.1-flash-lite"
    elif os.environ.get("GROQ_API_KEY"):
        _cached_model = "groq:qwen/qwen3-32b"
    elif os.environ.get("MISTRAL_API_KEY"):
        _cached_model = "mistral:mistral-large-2512"
    elif os.environ.get("CEREBRAS_API_KEY"):
        _cached_model = "cerebras:zai-glm-4.7"
    else:
        raise RuntimeError(
            "No AI provider configured. Set at least one supported provider key: "
            "ANTHROPIC_API_KEY, OPENAI_API_KEY, JAMS_API_KEY, HCAI_API_KEY, "
            "OPENROUTER_API_KEY, OPENROUTER_API_KEY_FALLBACK, GOOGLE_API_KEY, "
            "GROQ_API_KEY, MISTRAL_API_KEY, or CEREBRAS_API_KEY."
        )
    return _cached_model


def _apply_provider_env(provider_name: str, api_key: str) -> None:
    """Set the provider API key env var pydantic-ai needs to instantiate a model.

    Mirrors the env setup used inside run_agent so other agents (e.g. kevinton)
    select the same provider and have its key available.
    """
    if provider_name in ("byok", "hcai", "hcai_minimax", "hcai_luna"):
        return  # BYOK / HCAI use an explicit base_url + api_key at model creation
    if not api_key:
        return
    if provider_name == "anthropic":
        os.environ["ANTHROPIC_API_KEY"] = api_key
    elif provider_name == "openai":
        os.environ["OPENAI_API_KEY"] = api_key
    elif provider_name in ("jams", "openrouter_fb", "jams_luna"):
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
    """Resolve the provider model AND set its env key, like run_agent does.

    Returns the model string for the first viable provider in the fallback order
    (BYOK user endpoint first when present), or a fully-configured model object
    for providers with a custom base_url (BYOK/HCAI). Raises RuntimeError if none
    configured.
    """
    provider_order = _build_provider_order(deps_user_id)
    for provider_name, provider_config in provider_order:
        api_key = provider_config.get("api_key")
        if not api_key and provider_name != "byok":
            continue
        model_name = provider_config["model"]
        if provider_config.get("base_url"):
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider
            return OpenAIChatModel(
                model_name,
                provider=OpenAIProvider(
                    base_url=provider_config["base_url"],
                    api_key=provider_config["api_key"],
                ),
            )
        _apply_provider_env(provider_name, api_key or "")
        return model_name
    raise RuntimeError(
        "No AI provider configured. "
        "Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or JAMS_API_KEY."
    )


_VISION_MODEL_MARKERS = (
    "claude",
    "gpt-4",
    "gpt-5",
    "o1",
    "o3",
    "o4",
    "luna",
    "gemini",
    "kimi",
    "minimax",
    "gemma",
    "versatile",
    "llava",
    "vision",
    "vlm",
    "-vl",
)


def _is_vision_capable(model_name: str) -> bool:
    """Best-effort check of whether a model string supports image input."""
    m = model_name.lower()
    if m.startswith("anthropic:"):
        return True
    return any(marker in m for marker in _VISION_MODEL_MARKERS)


def _resolve_provider_order(deps_user_id: str | None = None) -> list:
    """The provider fallback order the run loop will actually try, cache-adjusted.

    Applies the global fallback cache (skip dead providers, prefer the
    last-known-good provider first) the same way _run_with_provider_chain does,
    so the vision gate in run_agent agrees with the model that runs.
    """
    from agent.fallback_cache import get_dead_providers, get_working_provider

    provider_order = _build_provider_order(deps_user_id)
    if not provider_order:
        raise RuntimeError("No AI provider configured.")

    has_byok = provider_order[0][0] == "byok"
    if not has_byok:
        dead_providers = get_dead_providers()
        if dead_providers:
            alive = [(n, c) for n, c in provider_order if n not in dead_providers]
            skipped = len(provider_order) - len(alive)
            if skipped:
                logger.info(f"Fallback cache: skipping {skipped} dead provider(s): {sorted(dead_providers)}")
            provider_order = alive or provider_order

        cached_provider = get_working_provider()
        if cached_provider:
            for i, (name, _) in enumerate(provider_order):
                if name == cached_provider:
                    provider_order.insert(0, provider_order.pop(i))
                    logger.info(f"Fallback cache: trying {cached_provider} first (global)")
                    break
    return provider_order


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
        provider_order.append(("jams_luna", {"model": "openrouter:openai/gpt-5.6-luna", "base_url": None, "api_key": JAMS_API_KEY}))
    HCAI_API_KEY = os.environ.get("HCAI_API_KEY")
    if HCAI_API_KEY:
        provider_order.append(("hcai_luna", {"model": "openai/gpt-5.6-luna", "base_url": "https://ai.hackclub.com/proxy/v1", "api_key": HCAI_API_KEY}))
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


def get_user_text_endpoint(user_id: str | None) -> dict | None:
    """Get the full endpoint config for a user's text endpoint, or None."""
    if not user_id:
        return None
    ep_id = get_text_endpoint_id(user_id)
    if not ep_id:
        return None
    return get_endpoint_decrypted(user_id, ep_id)


def _redact_tool_result(ctx, *, call, tool_def, args, result):
    if isinstance(result, str):
        return _redact(result, context=f"tool {tool_def.name}")
    return result


def _redact_output(ctx, *, output_context, output):
    if isinstance(output, str):
        return _redact(output, context="final response")
    return output


_hooks = Hooks(
    after_tool_execute=_redact_tool_result,
    after_output_process=_redact_output,
)


agent = Agent(
    deps_type=AgentDeps,
    system_prompt=SYSTEM_PROMPT,
    tools=[add_emoji_reaction],
    capabilities=[_hooks],
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
    if not bot_token:
        return "Error: SLACK_BOT_TOKEN not configured."
        
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
        sandbox, proxy_info = get_or_create_sandbox(channel_id, thread_ts)
        # Pass the GitHub proxy env directly (E2B `envs=`) so gh/git/curl are authenticated
        # via the host proxy on every command; the real token never enters the sandbox.
        try:
            result = sandbox.commands.run(command, envs=_proxy_env(proxy_info))
        finally:
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


# Tools a `code_mode` program may NOT call: recursion back into the sandbox, the tool itself,
# or run-control tools that make no sense from a sandboxed loop.
CODE_MODE_EXCLUDED_TOOLS = {
    "code_mode",
    "run_linux_command",
    "read_sandbox_file_tool",
    "write_sandbox_file_tool",
    "search_sandbox_files_tool",
    "list_sandbox_files_tool",
    "download_attachments_to_sandbox",
    "extract_tar_gz_tool",
    "analyze_csv_tool",
    "run_sql_on_csv_tool",
    "run_python_data_analysis_tool",
    "install_opencode_tool",
    "run_opencode_tool",
    "upload_file_from_sandbox",
    "skip",
    "leave_thread_tool",
    "join_thread_tool",
    "add_emoji_reaction",
    "delegate_to_subagent",
}


def _code_mode_tools() -> tuple[list[str], dict[str, str]]:
    registry = agent._function_toolset.tools
    allowlist = [n for n in registry if n not in CODE_MODE_EXCLUDED_TOOLS]
    signatures = format_signatures({n: registry[n] for n in allowlist})
    return allowlist, signatures


def _tool_resolver(tool_name: str):
    td = agent._function_toolset.tools.get(tool_name)
    return td.function if td else None


@agent.tool
def code_mode(ctx: RunContext[AgentDeps], code: str) -> str:
    """Run a Python program in your sandbox where you can call your own tools programmatically.

    Use this when you need to repeat a tool call many times (looping over API results, batch
    checks, bulk Slack operations) -- it runs on the sandbox without burning model tokens per
    call. Write a program that `import agent_tools` and calls tools as
    `agent_tools.<tool_name>(*args)`. `agent_tools.help()` lists the allowed tools + signatures.

    Allowed tools exclude the sandbox tools and `code_mode` itself. The generic Slack API tools
    `slack_api_call` and `slack_api_call_as_bot_tool` return parsed JSON dicts (iterate over
    them directly). Most other tools return descriptive strings. Each tool call is executed on
    the host with the current thread's credentials and posts/reads the same channel/thread.

    Example - find bots among channel members:
    ```python
    import agent_tools
    members = agent_tools.slack_api_call_as_bot_tool(
        "conversations.members", {"channel": "C0B7QEK0MQB"}
    )["members"]
    bots = []
    for uid in members:
        info = agent_tools.slack_api_call_as_bot_tool("users.info", {"user": uid})
        if info.get("user", {}).get("is_bot"):
            bots.append(uid)
    print(len(bots), "bots:", bots)
    ```

    Args:
        code: The full Python source to run.
    """
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured."
    channel_id = ctx.deps.channel_id
    thread_ts = ctx.deps.thread_ts
    try:
        start_tool_proxy()
        sandbox, proxy_info = get_or_create_sandbox(channel_id, thread_ts)

        allowlist, signatures = _code_mode_tools()
        register_sandbox(sandbox.sandbox_id, proxy_info["token"], ctx.deps, _tool_resolver, allowlist)
        sandbox.files.write("/home/user/agent_tools.py", build_sandbox_module(allowlist, signatures))
        sandbox.files.write("/home/user/code_mode_run.py", code)
        envs = dict(_proxy_env(proxy_info))
        envs.update({
            "AGENT_TOOLS_BASE": f"https://{PUBLIC_PROXY_HOST}/agent_tools",
            "AGENT_TOOLS_TOKEN": proxy_info["token"],
            "AGENT_TOOLS_SANDBOX": sandbox.sandbox_id,
        })
        try:
            result = sandbox.commands.run(
                "cd /home/user && python3 code_mode_run.py", timeout=600, envs=envs
            )
        finally:
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


def download_slack_attachments(
    channel_id: str, thread_ts: str, sandbox: "Sandbox",
    user_token: str | None = None, limit: int = 20,
) -> str:
    """Download files attached to messages in this thread only.

    Uses conversations.replies so files are scoped to the thread's own
    messages, never files shared elsewhere in the channel.
    """
    token = user_token or os.environ.get("SLACK_USER_TOKEN")
    if not token:
        return "Error: SLACK_USER_TOKEN not configured"
    sandbox.commands.run("mkdir -p ~/attachments")
    url = "https://slack.com/api/conversations.replies"
    headers = {"Authorization": f"Bearer {token}"}

    files = []
    cursor = None
    try:
        while len(files) < limit:
            params = {"channel": channel_id, "ts": thread_ts, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            response = requests.get(url, headers=headers, params=params)
            res_json = response.json()
            if not res_json.get("ok"):
                return f"Slack API error: {res_json.get('error', 'unknown')}"
            messages = res_json.get("messages", [])
            for message in messages:
                for f in message.get("files") or []:
                    files.append(f)
                    if len(files) >= limit:
                        break
                if len(files) >= limit:
                    break
            cursor = (res_json.get("response_metadata") or {}).get("next_cursor")
            if not cursor or not messages:
                break

        if not files:
            return "No files found in this thread."
        results = []
        for f in files[:limit]:
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
        sandbox, _ = get_or_create_sandbox(channel_id, thread_ts)
        return download_slack_attachments(channel_id, thread_ts, sandbox, user_token)
    except Exception as e:
        return f"Error: {str(e)}"


@agent.tool
def get_slack_file_tool(ctx: RunContext[AgentDeps], file: str, filename: str = "") -> str:
    """Download a Slack file (upload, snippet, image, canvas, any type) into the sandbox by file id.

    Takes a Slack file id (e.g. F0123ABCD), which you can get from a message attachment or a
    Slack file permalink. Not for arbitrary web URLs; use fetch_url for those. When downloading
    images, pass a filename with the correct extension (.png, .jpg, .jpeg, .webp).
    NEVER guess the file id — pull the real F... id from the message's attachments or permalink.

    Args:
        file: Slack file id (e.g. F0123ABCD), or a Slack file permalink containing the id.
        filename: Optional name to save it as (defaults to the file's own name).
    """
    from agent.tools.slack_file_download import download_file_by_id

    user_token = ctx.deps.user_token or os.environ.get("SLACK_USER_TOKEN")
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured"
    if not user_token:
        return "Error: SLACK_USER_TOKEN not configured"

    sandbox = None
    sandbox_id = get_thread_sandbox_id(ctx.deps.channel_id, ctx.deps.thread_ts)
    if sandbox_id:
        try:
            sandbox = Sandbox.connect(sandbox_id)
        except Exception as e:
            return f"Error connecting to sandbox: {e}"
    return download_file_by_id(file, user_token, sandbox, filename=filename)


@agent.tool
def upload_file_from_sandbox(
    ctx: RunContext[AgentDeps], filepath: str, title: str = "", initial_comment: str = "",
) -> str:
    """Upload a file from the sandbox and post its hosted link in the current channel/thread.

    Files are hosted on the coolton file server (tanjim.org:2390), so there is
    no size limit. Slack's own file upload API silently drops shares in this
    workspace, so the file is served from the coolton server instead and the
    link is posted with chat.postMessage.
    """
    channel_id = ctx.deps.channel_id
    thread_ts = ctx.deps.thread_ts
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured"
    try:
        sandbox, _ = get_or_create_sandbox(channel_id, thread_ts)
        file_content = bytes(sandbox.files.read(filepath, format="bytes"))
        if file_content is None:
            return f"Error: File not found at {filepath}"
        filename = os.path.basename(filepath)

        from agent.web64_client import upload_bytes

        url = upload_bytes(file_content, filename)
        label = title or filename
        message = f"{initial_comment}\n\n📄 *{label}*: {url}" if initial_comment else f"📄 *{label}*: {url}"
        post_kwargs = {"channel": channel_id, "text": message}
        if thread_ts:
            post_kwargs["thread_ts"] = thread_ts
        post_resp = ctx.deps.client.chat_postMessage(**post_kwargs)
        if not post_resp.get("ok"):
            return f"File hosted at {url}, but posting the link failed: {post_resp.get('error', 'unknown')}"
        return f"Uploaded {filename} and posted the link in the thread."
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
    try:
        sandbox, _ = get_or_create_sandbox(channel_id, thread_ts)
        image_data = bytes(sandbox.files.read(image_path, format="bytes"))
        if image_data is None:
            return f"Error: File not found at {image_path}"
        from agent.tools.vision import analyze_image
        filename = os.path.basename(image_path)
        return analyze_image(image_data, filename, prompt)
    except Exception as e:
        return f"Error analyzing image: {str(e)}"


_IMAGE_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}


@agent.tool
def see_image_from_sandbox(ctx: RunContext[AgentDeps], path: str) -> ToolReturn[str]:
    """View an image file that is in your sandbox.

    You can actually SEE the image: its pixels are sent back to your vision model.
    Use this to read text off a screenshot, describe a chart or photo, check a
    generated/downloaded image, extract details from a diagram, etc. Only works on
    image files (png, jpg, jpeg, gif, webp, bmp).

    Args:
        path: Path to the image in the sandbox (e.g. ~/downloads/photo.png or
              /home/user/attachments/screenshot.jpg).
    """
    if not os.environ.get("E2B_API_KEY"):
        return ToolReturn("Error: E2B_API_KEY not configured")
    path = path.strip()
    if path.startswith("~/"):
        path = "/home/user/" + path[2:]
    elif not path.startswith("/"):
        path = "/home/user/" + path
    if ".." in path.split("/"):
        return ToolReturn("Error: relative paths (..) are not allowed.")
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    mime = _IMAGE_MIME_BY_EXT.get(ext)
    if not mime:
        return ToolReturn(
            f"Error: unsupported file type '.{ext}'. Supported: png, jpg, jpeg, gif, webp, bmp."
        )
    try:
        sandbox, _ = get_or_create_sandbox(ctx.deps.channel_id, ctx.deps.thread_ts)
        data = bytes(sandbox.files.read(path, format="bytes"))
    except Exception as e:
        return ToolReturn(f"Error reading {path} from sandbox: {e}")
    if not data:
        return ToolReturn(f"Error: no file found at {path}")
    if len(data) > 15 * 1024 * 1024:
        return ToolReturn(f"Error: {path} is {len(data)} bytes; images over 15MB can't be sent to the model.")
    return ToolReturn(
        f"Here is the image from your sandbox at {path} ({len(data)} bytes, {mime}).",
        content=[
            f"(Image from {path} via see_image_from_sandbox)",
            BinaryContent(data=data, media_type=mime, vendor_metadata={"detail": "high"}),
        ],
    )


@agent.tool
def generate_image_tool(
    ctx: RunContext[AgentDeps],
    prompt: str,
    n: int = 1,
    size: str = "1024x1024",
    aspect_ratio: str = "",
) -> str:
    """Generate AI images from a text prompt using an OpenAI-compatible image model.

    The images are saved into the sandbox ~/downloads/ directory (if a sandbox is active)
    and their URLs are returned. Use upload_file_from_sandbox to send them to Slack.

    Requires the user to have an OpenAI API key (via BYOK or global OPENAI_API_KEY).

    Args:
        prompt: Text description of the desired image.
        n: Number of images (1-4, default 1).
        size: Size ("1024x1024", "1792x1024", "1024x1792", default "1024x1024").
        aspect_ratio: Optional aspect ratio like "16:9", "1:1", "9:16", "4:3".
            Overrides size when it maps to a known size; otherwise passed through
            to providers that support an `aspect_ratio` field.
    """
    from agent.tools.image_gen import generate_image_with_byok, save_images_to_sandbox

    result = generate_image_with_byok(
        ctx.deps.user_id, prompt, n, size, aspect_ratio or None
    )
    if "image(s)" not in result:
        return result

    if os.environ.get("E2B_API_KEY"):
        sandbox = None
        sandbox_id = get_thread_sandbox_id(ctx.deps.channel_id, ctx.deps.thread_ts)
        if sandbox_id:
            try:
                sandbox = Sandbox.connect(sandbox_id)
            except Exception:
                sandbox = None
        if sandbox:
            urls = [line.split(". ", 1)[-1] for line in result.splitlines()[1:] if line]
            saved = save_images_to_sandbox(sandbox, urls)
            if saved:
                return result + "\n\nSaved to sandbox:\n" + "\n".join(f"- {p}" for p in saved)
    return result


def _post_image_to_channel(channel_id: str, thread_ts: str, image_url: str, alt_text: str) -> str | None:
    """Post an image block to a channel/thread. Returns None on success or an error string."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        return "Error: SLACK_BOT_TOKEN not configured"
    blocks = [{"type": "image", "image_url": image_url, "alt_text": alt_text}]
    payload = {"channel": channel_id, "text": alt_text, "blocks": blocks}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
            timeout=20,
        )
        res_json = response.json()
        if res_json.get("ok"):
            return None
        return f"Error posting diagram: {res_json.get('error', 'unknown')}"
    except Exception as e:
        return f"Error posting diagram: {str(e)}"


@agent.tool
def render_mermaid_tool(ctx: RunContext[AgentDeps], diagram_code: str, theme: str = "default") -> str:
    """Render a Mermaid diagram and post the PNG image into the current thread.

    Supports: flowcharts, sequence diagrams, class diagrams, state diagrams,
    Gantt charts, pie charts, entity relationship diagrams, user journey, etc.
    The rendered image is posted directly into the current channel/thread.

    Args:
        diagram_code: Mermaid diagram definition (e.g., "graph TD; A-->B;").
        theme: Theme ("default", "dark", "forest", "neutral", default "default").
    """
    from agent.tools.mermaid_tool import render_mermaid
    url = render_mermaid(diagram_code, theme)
    if not url.startswith("http"):
        return url
    error = _post_image_to_channel(ctx.deps.channel_id, ctx.deps.thread_ts, url, "Mermaid diagram")
    if error:
        return f"{error} | url: {url}"
    return f"Diagram rendered and posted to the thread: {url}"


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
def create_scheduled_task_tool(ctx: RunContext[AgentDeps], prompt: str, cron: str, timezone: str = "UTC") -> str:
    """Create a recurring scheduled task that posts `prompt` to this thread/channel on a cron schedule.

    The task fires in the exact Slack thread (or channel) where it was created.
    Cron expressions must run at least 30 minutes apart (no more often than every 30 min).

    Args:
        prompt: The instruction/message text to post each time the task fires.
        cron: Standard 5-field cron expression (e.g. '0 9 * * *' = daily 9:00).
        timezone: IANA timezone name (default 'UTC', e.g. 'Asia/Dhaka').
    """
    from agent.scheduler import create_scheduled_task
    return create_scheduled_task(
        ctx.deps.user_id, ctx.deps.channel_id, ctx.deps.thread_ts, prompt, cron, timezone
    )


@agent.tool
def list_scheduled_tasks_tool(ctx: RunContext[AgentDeps], view_all: bool = False) -> str:
    """List your recurring scheduled tasks (id, status, cron, next/last run).

    Args:
        view_all: Only admins can view everyone's tasks; non-admins are ignored.
    """
    from agent.scheduler import list_scheduled_tasks
    return list_scheduled_tasks(ctx.deps.user_id, view_all)


@agent.tool
def pause_scheduled_task_tool(ctx: RunContext[AgentDeps], task_id: str) -> str:
    """Pause a recurring scheduled task you created (stops future runs).

    Args:
        task_id: The task id from list_scheduled_tasks_tool.
    """
    from agent.scheduler import pause_scheduled_task
    return pause_scheduled_task(ctx.deps.user_id, task_id)


@agent.tool
def resume_scheduled_task_tool(ctx: RunContext[AgentDeps], task_id: str) -> str:
    """Resume a paused recurring scheduled task.

    Args:
        task_id: The task id from list_scheduled_tasks_tool.
    """
    from agent.scheduler import resume_scheduled_task
    return resume_scheduled_task(ctx.deps.user_id, task_id)


@agent.tool
def delete_scheduled_task_tool(ctx: RunContext[AgentDeps], task_id: str) -> str:
    """Delete a recurring scheduled task you created. Permanent.

    Args:
        task_id: The task id from list_scheduled_tasks_tool.
    """
    from agent.scheduler import delete_scheduled_task
    return delete_scheduled_task(ctx.deps.user_id, task_id)


@agent.tool
def fetch_url_tool(ctx: RunContext[AgentDeps], url: str, max_characters: int = 8000) -> str:
    """Fetch the readable text content of a specific URL (like web_search but for a known link).

    Use when the user shares a URL and wants its content summarized or read,
    or when you need the full text of a page found via search_web.

    Args:
        url: The full URL to fetch.
        max_characters: Max characters of text to return (default 8000).
    """
    from agent.tools.web_search import fetch_url
    return fetch_url(url, max_characters)


@agent.tool
def get_user_tool(ctx: RunContext[AgentDeps], user_id: str) -> str:
    """Look up a Slack user's profile: display name, real name, pronouns, timezone, title, status, custom fields.

    Use their pronouns! Handy for onboarding or addressing people correctly.

    Args:
        user_id: Slack user ID (U...).
    """
    from agent.tools.slack_info import get_user_info
    return get_user_info(user_id)


@agent.tool
def get_channel_info_tool(ctx: RunContext[AgentDeps], channel_id: str) -> str:
    """Look up Slack channel metadata: name, type (public/private/DM), member count, topic, purpose.

    Args:
        channel_id: Slack channel ID (C..., D..., or G...).
    """
    from agent.tools.slack_info import get_channel_info
    return get_channel_info(channel_id)


@agent.tool
def post_message_tool(ctx: RunContext[AgentDeps], channel_id: str, text: str, thread_ts: str = "") -> str:
    """Post a message as coolton to a Slack channel/thread — but ONLY to the current channel
    (or a thread within it), or a DM with the user who asked. Posting elsewhere is refused.

    Use when the user explicitly asks you to post somewhere mid-turn (progress updates,
    standalone posts). For replies in the current thread, prefer the final response instead.

    Args:
        channel_id: Target channel ID.
        text: Message text (Markdown supported).
        thread_ts: Optional thread timestamp to post into.
    """
    from agent.tools.slack_info import post_message_to_target
    return post_message_to_target(
        channel_id=channel_id, text=text, thread_ts=thread_ts,
        from_user=ctx.deps.user_id, current_channel=ctx.deps.channel_id,
    )


@agent.tool
def leave_channel_tool(ctx: RunContext[AgentDeps], channel_id: str = "") -> str:
    """Make coolton leave a Slack channel (cannot leave DMs).

    Use when the user asks coolton to leave/be removed from a channel. Not usable in DMs.

    Args:
        channel_id: Channel to leave (defaults to the current channel).
    """
    from agent.tools.slack_info import leave_slack_channel
    return leave_slack_channel(channel_id or ctx.deps.channel_id)


@agent.tool
def remove_reaction_tool(ctx: RunContext[AgentDeps], emoji_name: str, timestamp: str = "") -> str:
    """Remove an emoji reaction from a message.

    Args:
        emoji_name: Emoji name without colons (e.g. 'tada').
        timestamp: Message ts to remove the reaction from (defaults to the current message).
    """
    from agent.tools.slack_info import remove_emoji_reaction
    return remove_emoji_reaction(ctx.deps.channel_id, timestamp or ctx.deps.message_ts, emoji_name)


@agent.tool
def search_slack_tool(ctx: RunContext[AgentDeps], query: str, count: int = 5) -> str:
    """Search Slack messages across the workspace (channels, DMs, files) with the user token.

    Supports Slack search syntax like `in:#channel from:@user` and keywords.

    Args:
        query: The search query.
        count: Number of results to return (default 5, max 20).
    """
    from agent.tools.slack_search import search_slack_messages
    return search_slack_messages(query, count)


@agent.tool
def read_conversation_history_tool(
    ctx: RunContext[AgentDeps], channel_id: str, limit: int = 20, cursor: str = "", thread_ts: str = ""
) -> str:
    """Read recent messages from a Slack channel, or replies within a thread.

    Use to catch up on a channel or thread you haven't seen. Returns a next_cursor
    when there is more; call again with it to read older messages.

    Args:
        channel_id: The channel ID to read.
        limit: Number of messages (default 20, max 200).
        cursor: Pagination cursor for older messages.
        thread_ts: If set, read replies in that thread instead of the channel.
    """
    from agent.tools.slack_search import read_conversation_history
    return read_conversation_history(channel_id, limit, cursor, thread_ts, current_channel_id=ctx.deps.channel_id)


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
    whiteboard_id: str | None = None, user_token: str | None = None,
) -> str:
    if whiteboard_id is None:
        whiteboard_id = f"{random.randint(0, 0xFFFFFF):06X}"
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
    title: str = "whiteboard", whiteboard_id: str | None = None,
) -> str:
    """Send a Felix whiteboard (tldraw) embed to the current channel.
    
    Creates a new whiteboard with a random ID at felix's tldraw instance.
    
    Args:
        text: Fallback text (default: "whiteboard").
        title: Embed title (default: "whiteboard").
        whiteboard_id: Optional specific 6-digit uppercase hex ID like "3A9F01" (default: random).
    """
    return send_whiteboard_embed(channel_id=ctx.deps.channel_id, text=text, title=title, whiteboard_id=whiteboard_id)


def send_html_embed(
    channel_id: str, html: str, text: str = "html embed", title: str = "html embed",
    user_token: str | None = None,
) -> str:
    from html import escape as _html_escape

    try:
        from agent.web64_client import upload_bytes
        if "<head" not in html.lower():
            meta = (
                f'<head><meta property="og:title" content="{_html_escape(title)}"/>'
                f'<meta property="og:description" content="{_html_escape(text)}"/></head>'
            )
            html = meta + html
        url = upload_bytes(html.encode(), "embed.html", mime="text/html")
    except Exception as e:
        return f"Error hosting HTML embed: {e}"
    thumbnail_url = "https://placehold.co/1280x720?text=click%20to%20open%20the%20\\ncoolton%20embed"
    return send_web_embed(channel_id=channel_id, text=text, url=url, title=title, thumbnail_url=thumbnail_url, user_token=user_token)


@agent.tool
def send_html_embed_tool(
    ctx: RunContext[AgentDeps], html: str, text: str = "html embed",
    title: str = "html embed",
) -> str:
    """Send custom HTML as a live embed in the current channel.

    Your HTML is hosted on the coolton file server (2390.proxy.tanjim.org) as a
    short URL and sent as a Slack embed (same mechanism as the whiteboard embed).
    There is no size limit.

    IMPORTANT: the embed's default background varies (it can be black, white, or
    the viewer's theme), so NEVER rely on default colors — always set an explicit
    background-color AND text color in the CSS (e.g. a styled <body> or <div>
    wrapper), otherwise text can be invisible (e.g. black text on a black
    background).

    Args:
        html: Raw HTML content.
        text: Fallback text (default: "html embed").
        title: Embed title (default: "html embed").
    """
    return send_html_embed(channel_id=ctx.deps.channel_id, html=html, text=text, title=title)


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
    if method.startswith("apps.manifest."):
        return f"Error: {method} requires a Slack App Configuration Token (xoxe), not a user token. Use the create_slack_bot tool instead."
    if method == "chat.postMessage":
        if not params.get("channel"):
            return "Error: chat.postMessage requires a 'channel' (channel id or user id for a DM) param — use the chat_postMessage tool instead."
        if not params.get("text"):
            return "Error: chat.postMessage requires a 'text' param — use the chat_postMessage tool instead."
    url = f"https://slack.com/api/{method}"
    headers = {"Authorization": f"Bearer {user_token}"}
    form = {
        k: json.dumps(v) if isinstance(v, (dict, list)) else v
        for k, v in params.items()
    }
    try:
        response = requests.post(url, data=form, headers=headers)
        res_json = _strip_secret_keys(response.json())
        if res_json.get("ok"):
            return f"Success: {_redact(str(res_json), context='slack_api_call')}"
        return f"Slack API error: {_redact(str(res_json), context='slack_api_call')}"
    except Exception as e:
        return f"Error: {_redact(str(e), context='slack_api_call')}"


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
def create_slack_bot_tool(ctx: RunContext[AgentDeps], manifest: dict) -> str:
    """Create a Slack app from a manifest. Returns app_id and OAuth install URL.
    
    Uses the xoxe config token. The manifest must include display_information.name.
    After creating, visit the oauth_authorize_url to install the app, then use
    register_bot_tokens to store the resulting bot/app tokens.
    
    Args:
        manifest: Slack app manifest dict with display_information, features, etc.
    """
    from agent.tools.slack_bot_deploy import create_slack_bot
    return create_slack_bot(manifest)


@agent.tool
def register_bot_tokens_tool(ctx: RunContext[AgentDeps], uuid: str, bot_token: str, app_token: str, signing_secret: str = "") -> str:
    """Store bot/app tokens for a created Slack app. Only xoxb- bot tokens and xapp- app tokens accepted.
    
    Args:
        uuid: The app_id returned by create_slack_bot.
        bot_token: The xoxb- bot token from the installed app.
        app_token: The xapp- app-level token.
        signing_secret: The signing secret from the app credentials (optional).
    """
    from agent.tools.slack_bot_deploy import register_bot_tokens
    return register_bot_tokens(uuid, bot_token, app_token, signing_secret)


@agent.tool
def wrangler_bot_deploy_tool(ctx: RunContext[AgentDeps], uuid: str, working_dir: str, additional_flags: str = "") -> str:
    """Deploy a Slack bot Worker using wrangler. Injects stored tokens, runs deploy, then deletes the secrets file.
    
    Args:
        uuid: The app_id from create_slack_bot.
        working_dir: Directory containing the bot code.
        additional_flags: Extra flags for wrangler deploy (e.g. "--minify").
    """
    from agent.tools.slack_bot_deploy import wrangler_bot_deploy
    return wrangler_bot_deploy(uuid, working_dir, additional_flags)


@agent.tool
def leave_thread_tool(ctx: RunContext[AgentDeps]) -> str:
    """Leave the current thread - ignore messages here until coolton is mentioned again.

    Use this when the user asks you to stop responding in a thread. A mid-thread
    mention still answers once but does not rejoin the thread.
    """
    from agent.leave_thread_store import leave_thread
    return leave_thread(ctx.deps.channel_id, ctx.deps.thread_ts)


@agent.tool
def join_thread_tool(ctx: RunContext[AgentDeps]) -> str:
    """Join the current thread - respond to every message here until told to leave.

    Normally coolton only joins a thread when its starter message mentions it;
    a mid-thread mention answers once without joining. Use this when the user
    asks you to stay in (or keep responding in) this thread.
    """
    from agent.leave_thread_store import join_thread
    return join_thread(ctx.deps.channel_id, ctx.deps.thread_ts)


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
            text=_redact(text, context="send_message"),
        )
        return "Message sent."
    except Exception as e:
        return f"Failed to send message: {_redact(str(e), context='send_message')}"


@agent.tool
def chat_postMessage(ctx: RunContext[AgentDeps], channel: str, text: str, thread_ts: str = "") -> str:
    """Send a Slack message to any channel or user as the coolton bot.

    Use this to DM a user (pass their user id as channel, e.g. `channel="U0B2VTYER33"`)
    or to post to a channel. For replying in the CURRENT thread, use send_message instead.

    Args:
        channel: Slack channel id, or a user id (U...) to open a DM.
        text: The message content (Markdown supported).
        thread_ts: Optional thread timestamp to post into a thread (omit for a top-level DM).
    """
    if not channel:
        return "Error: channel is required — pass the Slack channel id or user id."
    if not text:
        return "Error: text is required — provide the message content."
    try:
        kwargs = {"channel": channel, "text": _redact(text, context="chat_postMessage")}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = ctx.deps.client.chat_postMessage(**kwargs)
        if not resp.get("ok"):
            return f"Failed to send message: {resp.get('error', 'unknown')}"
        return "Message sent."
    except Exception as e:
        return f"Failed to send message: {_redact(str(e), context='chat_postMessage')}"


@agent.tool
def skip(ctx: RunContext[AgentDeps]) -> str:
    """Skip sending the final response message at the end of your turn.
    
    Use this when the user's request doesn't need a reply, when you've already
    responded via send_message, or when you have nothing to add.
    """
    ctx.deps.should_skip = True
    raise HaltRun("skip")


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


@agent.tool
def delegate_to_subagent(
    ctx: RunContext[AgentDeps],
    target: str,
    task: str,
) -> str:
    """Delegate a focused subtask to a specialized subagent and return its findings.

    Use this when a subtask is large, self-contained, and benefits from focused tools:
    - "research": focused Slack/web/user/channel/thread research, returns compact sourced findings.
    - "explore": inspect sandbox workspace files (read/list/grep) to gather implementation context.
    - "summarizer": summarize a Slack conversation transcript, preserving decisions and action items.

    Args:
        target: One of "research", "explore", "summarizer".
        task: A fully self-contained instruction describing exactly what to investigate or produce.
    """
    from agent.subagents import run_subagent

    return run_subagent(target, task, ctx.deps)


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
    desc = description.replace("'", "''")
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


class _SkipResult:
    """Minimal run result for a turn that was halted (skip / !stop)."""

    output = ""

    def __init__(self, history=None):
        self._history = history or []

    def all_messages(self):
        return self._history


def run_agent(text, deps, message_history=None, images=None):
    # Mark when this run started so a later !stop from the same user halts us.
    deps.run_started_at = time.time()

    # Attribute the incoming message to its sender so the model can tell users apart.
    platform = deps.platform or SlackPlatform(deps.client)
    text = platform.format_user_message(text, deps)

    from listeners.actions.instructions_actions import get_user_instructions as _get_instructions
    custom_instructions = _get_instructions(deps.user_id)
    deps.custom_instructions = custom_instructions

    # The model that will actually run (cache-adjusted provider order) decides whether
    # the agent is vision-capable: attached images are passed straight to a vision model,
    # and see_image_from_sandbox is only exposed to one.
    try:
        provider_order = _resolve_provider_order(deps.user_id)
        first_model = provider_order[0][1]["model"]
    except Exception:
        first_model = ""
    is_vision = _is_vision_capable(first_model)

    context_info = platform.build_context_prompt(deps, first_model, is_vision)
    full_prompt = platform.system_prompt + context_info
    if custom_instructions:
        full_prompt += f"\n\n## USER'S CUSTOM INSTRUCTIONS\n{custom_instructions}\n"

    deps.user_token = deps.user_token or os.environ.get("SLACK_USER_TOKEN")
    toolsets = platform.toolsets(deps)

    all_tools = list(agent._function_toolset.tools.values())
    if not is_vision:
        all_tools = [t for t in all_tools if t.name != "see_image_from_sandbox"]
    tool_functions = [t.function for t in all_tools]

    agent_dynamic = Agent(
        deps_type=AgentDeps,
        system_prompt=full_prompt,
        tools=tool_functions,
    )

    capabilities = [_hooks, PrepareTools(disable_strict_for_all_tools)]
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

    user_prompt: str | list = text
    if images and is_vision:
        user_prompt = [
            text,
            *[
                BinaryContent(
                    data=img["data"],
                    media_type=img["media_type"],
                    vendor_metadata={"detail": "high"},
                )
                for img in images
            ],
        ]

    run_kwargs = dict(
        user_prompt=user_prompt,
        deps=deps,
        message_history=message_history,
        toolsets=toolsets,
        capabilities=capabilities,
    )

    try:
        result, _provider = _run_with_provider_chain(agent_dynamic, run_kwargs, deps)
        return result
    except HaltRun as e:
        deps.should_skip = True
        deps.halt_reason = str(e)
        return _SkipResult(message_history)


def _run_with_provider_chain(agent_dynamic, run_kwargs, deps):
    """Run an agent against the provider fallback chain, returning (result, provider_name).

    Shared by run_agent (main orchestrator) and subagents (research/explore/summarizer).
    Uses the global fallback cache: skips providers known to be dead and prefers the
    last-known-good provider first.
    """
    from agent.fallback_cache import set_working_provider, mark_dead

    # Provider fallback order: BYOK endpoint → Anthropic → OpenAI → OpenRouter → Cerebras
    provider_order = _resolve_provider_order(deps.user_id)

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
    hard_error_markers = [
        "401",
        "403",
        "404",
        "user not found",
        "invalid api key",
        "invalid_api_key",
        "does not exist",
        "model_not_found",
        "model not found",
        "unavailable for free",
        "authentication failed",
        "unauthorized",
    ]

    def is_retryable_error(error: Exception) -> bool:
        error_str = str(error).lower()
        return any(retryable in error_str.lower() for retryable in retryable_errors)

    def is_hard_error(error: Exception) -> bool:
        error_str = str(error).lower()
        return any(marker in error_str.lower() for marker in hard_error_markers)

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
                if provider_name not in ("byok", "hcai", "hcai_minimax", "hcai_luna") and provider_config.get("api_key"):
                    if provider_name == "anthropic":
                        os.environ["ANTHROPIC_API_KEY"] = provider_config["api_key"]
                    elif provider_name == "openai":
                        os.environ["OPENAI_API_KEY"] = provider_config["api_key"]
                    elif provider_name in ("jams", "openrouter_fb", "jams_luna"):
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

                run_kwargs["model"] = model_obj if model_obj else model_name
                result = agent_dynamic.run_sync(**run_kwargs)
                if provider_name != "byok":
                    set_working_provider(provider_name)
                deps.model_used = f"{provider_name} / {model_name}"
                return result, provider_name

            except HaltRun:
                raise

            except Exception as e:
                if is_fatal_error(e):
                    logger.critical(f"Fatal error in {provider_name}: {_redact(str(e), context='provider {provider_name}')}")
                    raise
                err = _redact(str(e), context=f"provider {provider_name}")
                all_errors.append(f"{provider_name}: {err}")
                if is_hard_error(e):
                    if provider_name != "byok":
                        mark_dead(provider_name, err)
                    logger.warning(f"{provider_name} failed with a hard error (marked dead): {err}")
                    break  # Don't retry auth/config errors; skip this provider
                if is_retryable_error(e) and attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"{provider_name} attempt {attempt + 1} failed with retryable error: {err}. Retrying in {delay}s...")
                    time.sleep(delay)
                    continue
                else:
                    logger.warning(f"{provider_name} failed (attempt {attempt + 1}/{max_retries}): {err}")
                    break  # Try next provider

        # All retries exhausted for this provider, try next provider
        logger.warning(f"Provider {provider_name} exhausted all retries, trying next provider...")

    # All providers failed
    errors_str = "\n".join(f"  - {err}" for err in all_errors)
    raise RuntimeError(f"All AI providers failed.\n{errors_str}")


def disable_strict_for_all_tools(ctx, tool_defs):
    return [replace(tool_def, strict=False) for tool_def in tool_defs]
