import logging
import os
import random
import re
import time
import json
import shlex
import shutil
import threading
import requests
from pydantic_ai import RunContext
from pydantic_ai import Agent, TextOutput, ToolOutput
from pydantic_ai.messages import (
    BinaryContent, ModelRequest, ModelResponse, SystemPromptPart, ToolCallPart, ToolReturn, UserPromptPart,
)
from pydantic_ai.capabilities import Hooks, PrepareTools, ProcessHistory
from pydantic_ai.toolsets import FunctionToolset
from dataclasses import replace
from typing import Literal
from agent.deps import AgentDeps
from agent.surface import get_surface as _surface
from agent.platforms.slack import SlackPlatform
from agent.stop_store import HaltRun
from agent.tools import add_emoji_reaction
from agent.tools.computer_use import computer_use as _computer_use_dispatch
from agent.tools.computer_use import computer_stream as _computer_stream_start
from agent.tools.agent_browser_stream import agent_browser_stream as _agent_browser_stream_start
from agent.byok_store import get_text_endpoint_id, get_endpoint_decrypted
from agent import provider_config
from agent.redact import redact as _redact, strip_secret_keys as _strip_secret_keys
from e2b import Sandbox
from e2b.exceptions import FileNotFoundException
from agent.sandbox_store import get_thread_sandbox_id
from agent.sandbox_helpers import get_or_create_sandbox, pause_if_idle, sandbox_use, _proxy_env
from agent import sandbox_keepalive
from agent.github_proxy_client import PUBLIC_PROXY_HOST
from agent.tool_proxy import (
    build_sandbox_module,
    format_signatures,
    register_sandbox,
    unregister_sandbox,
    start as start_tool_proxy,
)

logger = logging.getLogger(__name__)

rate_limit_lock = threading.Lock()
_last_request_time = 0.0
RATE_LIMIT_INTERVAL = 15.0

_user_info_cache: dict[str, tuple[str, str]] = {}


def _get_user_display_info(user_id: str) -> tuple[str, str]:
    """Fetch display_name and profile picture URL for a Slack user. Cached per turn."""
    if user_id in _user_info_cache:
        return _user_info_cache[user_id]
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    if not bot_token or not user_id:
        return ("", "")
    try:
        resp = requests.get(
            "https://slack.com/api/users.info",
            params={"user": user_id},
            headers={"Authorization": f"Bearer {bot_token}"},
            timeout=5,
        )
        data = resp.json()
        if data.get("ok"):
            user = data.get("user", {})
            profile = user.get("profile", {})
            name = profile.get("display_name") or profile.get("real_name") or user.get("name") or ""
            pfp = profile.get("image_72") or profile.get("image_48") or ""
            _user_info_cache[user_id] = (name, pfp)
            return (name, pfp)
    except Exception:
        pass
    return ("", "")



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
    _cached_model = provider_config.get_model_from_config()
    return _cached_model


def _apply_provider_env(provider_name: str, api_key: str) -> None:
    """Set the provider API key env var pydantic-ai needs to instantiate a model.

    Delegates to provider_config.apply_provider_env which reads from providers.json.
    """
    provider_config.apply_provider_env(provider_name, api_key)


def get_runtime_model(deps_user_id: str | None = None) -> str:
    """Resolve the provider model AND set its env key, like run_agent does.

    Returns the model string for the first viable provider in the fallback order
    (BYOK user endpoint first when present), or a fully-configured model object
    for providers with a custom base_url (BYOK/HCAI). Raises RuntimeError if none
    configured.
    """
    provider_order = _build_provider_order(deps_user_id)
    for provider_name, prov_config in provider_order:
        api_key = prov_config.get("api_key")
        if not api_key and provider_name != "byok":
            continue
        model_name = prov_config["model"]
        if prov_config.get("base_url"):
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider
            return OpenAIChatModel(
                model_name,
                provider=OpenAIProvider(
                    base_url=prov_config["base_url"],
                    api_key=prov_config["api_key"],
                ),
            )
        _apply_provider_env(provider_name, api_key or "")
        return model_name
    raise RuntimeError(
        "No AI provider configured. "
        "Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or HCAI_API_KEY."
    )


def _resolve_provider_order(deps_user_id: str | None = None, tag: str | None = None) -> list:
    """The provider fallback order the run loop will actually try, cache-adjusted.

    Applies the global fallback cache (skip dead providers, prefer the
    last-known-good provider first) the same way _run_with_provider_chain does,
    so the vision gate in run_agent agrees with the model that runs.

    `tag`, when given, restricts the order to only tagged models (see
    agent.provider_config.extract_tag_directive) and skips the fallback
    cache's own reordering — a forced tag should not get silently overridden
    by "last known working provider."
    """
    from agent.fallback_cache import get_dead_families, get_dead_providers, get_working_provider

    provider_order = _build_provider_order(deps_user_id, tag)
    if not provider_order:
        raise RuntimeError("No AI provider configured.")

    # A family-wide outage (e.g. HCAI's whole account hitting its daily
    # spending cap) applies no matter what routed us here — unlike the
    # per-model dead cache below, skip it even under a forced [!WITH:tag] or
    # ahead of BYOK, since every model in that family is guaranteed to fail
    # the same way.
    dead_families = get_dead_families()
    if dead_families:
        alive = [
            (n, c) for n, c in provider_order
            if provider_config.provider_family(n) not in dead_families
        ]
        provider_order = alive or provider_order

    has_byok = provider_order[0][0] == "byok"
    if not has_byok and not tag:
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


def _build_provider_order(deps_user_id: str | None = None, tag: str | None = None) -> list:
    """Build the provider fallback order from providers.json."""
    return provider_config.build_provider_order(deps_user_id, tag)


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
        from agent.writing_style import strip_em_dashes

        from agent.mentions import defuse_mass_mentions

        return defuse_mass_mentions(strip_em_dashes(_redact(output, context="final response")))
    return output


def _redact_args(value, context: str):
    """`value` (a tool's arguments) with any secret value replaced, recursively."""
    if isinstance(value, str):
        return _redact(value, context=context)
    if isinstance(value, dict):
        return {k: _redact_args(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_args(v, context) for v in value]
    return value


async def _enforce_slack_budget(ctx, *, call, tool_def, args, handler):
    from agent.abuse_report import stopped_tool_call
    from agent.deferred_tools import shown_call
    from agent.slack_budget import spend

    tool_name = shown_call(call, args)[0].tool_name
    # A secret in a tool's arguments is always a leak (coolton's own tools read tokens from
    # the server's environment, never from arguments): the tool gets the redacted value,
    # not just the logs and plan block.
    args = _redact_args(args, f"tool input {tool_name}")
    # Nothing coolton posts may ping a group (@here, @channel, a user group...).
    from agent.mentions import defuse_args
    from agent.slack_budget import posts_to_slack
    if posts_to_slack(tool_name):
        args = defuse_args(args)
    # A request reported as abuse is stopped: nothing but declining is allowed after it.
    stopped = stopped_tool_call(ctx.deps, tool_name)
    if stopped:
        return stopped
    over = spend(ctx.deps, tool_name)
    if over:
        return over
    return await handler(args)


_hooks = Hooks(
    tool_execute=_enforce_slack_budget,
    after_tool_execute=_redact_tool_result,
    after_output_process=_redact_output,
)


agent = Agent(
    deps_type=AgentDeps,
    system_prompt=SYSTEM_PROMPT,
    tools=[add_emoji_reaction],
    capabilities=[_hooks],
)

# How the model reaches DEFERRED_TOOLS and the MCP servers (see agent.deferred_tools).
from agent.deferred_tools import CALL_TOOL, HiddenToolset, call_tool, search_tools  # noqa: E402

agent.tool(search_tools)
agent.tool(retries=2)(call_tool)

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
        response = requests.post(url, json=data, headers=headers, timeout=30)
        res_json = response.json()
        if res_json.get("ok"):
            return f"Success: Invited cooltonUser ({coolton_user_id}) to channel {channel_id}."
        error_code = res_json.get("error")
        if error_code == "already_in_channel":
            return "Notice: cooltonUser is already a member."
        return f"Failed to invite: {error_code}."
    except Exception as e:
        return f"Error: {str(e)}"


_RUN_LINUX_COMMAND_MIN_TIMEOUT = 10
_RUN_LINUX_COMMAND_MAX_TIMEOUT = 1800
_RUN_LINUX_COMMAND_DEFAULT_TIMEOUT = 60


def _run_in_thread_sandbox(ctx: RunContext[AgentDeps], use, command: str, timeout: int):
    """run_linux_command's command run, inside its sandbox_use block."""
    channel_id = ctx.deps.channel_id
    thread_ts = ctx.deps.thread_ts
    sandbox, proxy_info = get_or_create_sandbox(channel_id, thread_ts)
    use.sandbox = sandbox
    # Pass the GitHub proxy env directly (E2B `envs=`) so gh/git/curl are authenticated
    # via the host proxy on every command; the real token never enters the sandbox.
    # timeout=0 here means "disabled" (falsy timeout -> no deadline sent, per the E2B
    # SDK's own timeout_to_ms helper) — the model opts into that explicitly per call,
    # it isn't the implicit default.
    try:
        return sandbox.commands.run(command, envs=_proxy_env(proxy_info), timeout=timeout)
    finally:
        # A VNC stream (deps.sandbox_keepalive_seconds > 0) needs the sandbox to
        # survive between commands, not pause the instant this one returns — arm a
        # countdown instead (agent.sandbox_keepalive), reset on every action, so it
        # only actually pauses after real inactivity. A pending run_background_command
        # job on this thread needs the same thing for the same reason (see
        # agent.tools.sandbox_background's module docstring) — an unrelated
        # run_linux_command call must not freeze it back to zero progress. Otherwise
        # sandbox_use pauses it, unless another call is still using it.
        from agent.background_jobs_store import has_pending_jobs
        from agent.tools.sandbox_background import BG_JOB_KEEPALIVE_SECONDS
        has_bg_jobs = has_pending_jobs(channel_id, thread_ts)
        if ctx.deps.sandbox_keepalive_seconds > 0 or has_bg_jobs:
            use.pause = False
            ctx.deps.keep_sandbox_warm = True
            seconds = max(ctx.deps.sandbox_keepalive_seconds, BG_JOB_KEEPALIVE_SECONDS) if has_bg_jobs else ctx.deps.sandbox_keepalive_seconds
            sandbox_keepalive.arm(channel_id, thread_ts, seconds)


@agent.tool
def run_linux_command(ctx: RunContext[AgentDeps], command: str, timeout: int = _RUN_LINUX_COMMAND_DEFAULT_TIMEOUT) -> str:
    """Execute a bash/shell command inside a private cloud Linux sandbox (E2B).

    The sandbox PERSISTS across messages in your thread.

    Args:
        command: The shell command to run.
        timeout: Max seconds to let the command run before giving up (default 60,
            same as a quick shell command needs). Raise this BEFORE running anything
            you expect to be slow (agent-browser opening a page and waiting for it
            to load, npm installs, builds, long scripts), don't wait to find out
            from a "context deadline exceeded" error. Pass 0 to disable the timeout
            entirely and let the command run as long as it needs; only do this when
            you're confident it will actually finish on its own. Any other value is
            clamped to 10-1800 seconds.

    About to run `agent-browser open --headed <url>` for a nontrivial session? Call
    `agent_browser_stream_tool` first (before this command, not after), otherwise the
    browser opens invisibly on a desktop nobody's watching. This tool has no way to
    remind you again once the command is already running.
    """
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured."
    channel_id = ctx.deps.channel_id
    thread_ts = ctx.deps.thread_ts
    if timeout != 0:
        timeout = max(_RUN_LINUX_COMMAND_MIN_TIMEOUT, min(timeout, _RUN_LINUX_COMMAND_MAX_TIMEOUT))
    try:
        with sandbox_use(channel_id, thread_ts) as use:
            result = _run_in_thread_sandbox(ctx, use, command, timeout)
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
def run_background_command_tool(ctx: RunContext[AgentDeps], command: str, cwd: str = "") -> str:
    """Start a command in the sandbox running DETACHED IN THE BACKGROUND and
    return immediately with a job id, instead of blocking the turn until it
    finishes, use this for a dev server, a watcher, or any long-running
    process you need to keep alive while you do other things (check its
    output later with check_background_command_tool, stop it with
    kill_background_command_tool). For anything that just needs to finish and
    give you its output, use run_linux_command instead, don't background
    something you're only going to immediately wait on.

    You'll be notified automatically when it finishes, mid-turn as a steering
    note if you're still working, or as a fresh message if you've already
    finished responding, so there's no need to keep calling
    check_background_command_tool just to wait on it.

    Args:
        command: The shell command to run in the background.
        cwd: Directory to run it from (optional, defaults to the sandbox's
            default working directory).
    """
    from agent.tools.sandbox_background import run_background_command
    return run_background_command(ctx.deps.channel_id, ctx.deps.thread_ts, command, ctx.deps.user_id, cwd)


@agent.tool
def check_background_command_tool(
    ctx: RunContext[AgentDeps], job_id: str, tail_lines: int = 200,
) -> str:
    """Check whether a background command (started with
    run_background_command_tool) is still running, and see the most recent
    lines of its output.

    Args:
        job_id: The id returned by run_background_command_tool.
        tail_lines: How many of the most recent output lines to return
            (default 200).
    """
    from agent.tools.sandbox_background import check_background_command
    return check_background_command(ctx.deps.channel_id, ctx.deps.thread_ts, job_id, tail_lines)


@agent.tool
def kill_background_command_tool(ctx: RunContext[AgentDeps], job_id: str) -> str:
    """Stop a background command started with run_background_command_tool.

    Args:
        job_id: The id returned by run_background_command_tool.
    """
    from agent.tools.sandbox_background import kill_background_command
    return kill_background_command(ctx.deps.channel_id, ctx.deps.thread_ts, job_id)


# Tools a `code_mode` program may NOT call: recursion back into the sandbox, the tool itself,
# or run-control tools that make no sense from a sandboxed loop.
CODE_MODE_EXCLUDED_TOOLS = {
    "code_mode",
    "run_linux_command",
    "read_sandbox_file_tool",
    "write_sandbox_file_tool",
    "edit_sandbox_file_tool",
    "search_sandbox_files_tool",
    "list_sandbox_files_tool",
    "run_background_command_tool",
    "check_background_command_tool",
    "kill_background_command_tool",
    "download_attachments_to_sandbox",
    "extract_tar_gz_tool",
    "analyze_csv_tool",
    "run_sql_on_csv_tool",
    "run_python_data_analysis_tool",
    "install_opencode_tool",
    "run_opencode_tool",
    "upload_file_from_sandbox",
    "computer_use",
    "computer_stream_tool",
    "skip",
    "wait_tool",
    "leave_thread_tool",
    "join_thread_tool",
    "add_emoji_reaction",
    "delegate_to_subagent",
    "delegate_to_subagents",
    "create_code_channel_tool",
    # Need a live agent run; code mode already calls hidden tools directly by name.
    "search_tools",
    "call_tool",
    "report_abuse_tool",
}


def _code_mode_tools(excluded=None) -> tuple[list[str], dict[str, str]]:
    """The tools code_mode can call, minus `excluded` (a subagent's off-limits tools)."""
    registry = agent._function_toolset.tools
    allowlist = [n for n in registry if n not in CODE_MODE_EXCLUDED_TOOLS and n not in (excluded or ())]
    signatures = format_signatures({n: registry[n] for n in allowlist})
    return allowlist, signatures


_code_mode_locks: dict[tuple[str, str], threading.Lock] = {}
_code_mode_locks_lock = threading.Lock()


def _code_mode_lock(channel_id: str, thread_ts: str) -> threading.Lock:
    with _code_mode_locks_lock:
        return _code_mode_locks.setdefault((channel_id, thread_ts), threading.Lock())


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
        # One code_mode run per thread at a time: runs share the sandbox's proxy token,
        # its registration and the script files, so parallel ones would clobber each other.
        with _code_mode_lock(channel_id, thread_ts), sandbox_use(channel_id, thread_ts) as use:
            sandbox, proxy_info = get_or_create_sandbox(channel_id, thread_ts)
            use.sandbox = sandbox

            allowlist, signatures = _code_mode_tools(getattr(ctx.deps, "excluded_tools", None))
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
                # The tool-proxy registration only needs to live for this one run — the
                # sandboxed script has already exited by the time commands.run() returns,
                # so nothing can call back into /agent_tools/* with this token again.
                # Leaving it registered would let a leaked/replayed sandbox token keep
                # executing tools with THIS call's deps (this turn's WebClient, user_id,
                # user_token) indefinitely, and would grow _registrations without bound
                # across every code_mode call ever made. A later code_mode call on the
                # same (paused, not killed) sandbox re-registers fresh deps anyway.
                unregister_sandbox(sandbox.sandbox_id)
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
    messages, never files shared elsewhere in the channel. thread_ts="" means
    a code channel's channel-level conversation (see
    agent.code_channel_store) — there's no thread to scope to there, so this
    falls back to conversations.history (the channel's own top-level
    messages) instead.
    """
    token = user_token or os.environ.get("SLACK_USER_TOKEN")
    if not token:
        return "Error: SLACK_USER_TOKEN not configured"
    sandbox.commands.run("mkdir -p ~/attachments")
    url = (
        "https://slack.com/api/conversations.history" if not thread_ts
        else "https://slack.com/api/conversations.replies"
    )
    headers = {"Authorization": f"Bearer {token}"}

    files = []
    cursor = None
    try:
        while len(files) < limit:
            params = {"channel": channel_id, "limit": 200}
            if thread_ts:
                params["ts"] = thread_ts
            if cursor:
                params["cursor"] = cursor
            response = requests.get(url, headers=headers, params=params, timeout=30)
            res_json = response.json()
            if not res_json.get("ok"):
                return f"Slack API error: {res_json}"
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
            file_resp = requests.get(file_url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
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
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured"
    try:
        sandbox, _ = get_or_create_sandbox(channel_id, thread_ts)
        return _surface(ctx.deps).download_attachments(sandbox)
    except Exception as e:
        return f"Error: {str(e)}"


@agent.tool
def get_slack_file_tool(ctx: RunContext[AgentDeps], file: str, filename: str = "") -> str:
    """Download a Slack file (upload, snippet, image, canvas, any type) into the sandbox by file id.

    Takes a Slack file id (e.g. F0123ABCD), which you can get from a message attachment or a
    Slack file permalink. Not for arbitrary web URLs; use fetch_url for those. Only files
    shared in the current conversation or a public channel (or uploaded by the person
    asking) can be downloaded. When downloading
    images, pass a filename with the correct extension (.png, .jpg, .jpeg, .webp).
    NEVER guess the file id, pull the real F... id from the message's attachments or permalink.

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
    return download_file_by_id(
        file, user_token, sandbox, filename=filename,
        current_channel_id=ctx.deps.channel_id, requester_id=ctx.deps.user_id,
    )


@agent.tool
def upload_file_from_sandbox(
    ctx: RunContext[AgentDeps], filepath: str, title: str = "", initial_comment: str = "",
) -> str:
    """Upload a file from the sandbox and post its hosted link in the current channel/thread.

    Files are hosted on Bucky (bucky.hackclub.com), Hack Club's public file host.
    Slack's own file upload API silently drops shares in this workspace, so the
    file is hosted there instead and the link is posted with chat.postMessage.
    """
    channel_id = ctx.deps.channel_id
    thread_ts = ctx.deps.thread_ts
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured"
    try:
        sandbox, _ = get_or_create_sandbox(channel_id, thread_ts)
        try:
            file_content = bytes(sandbox.files.read(filepath, format="bytes"))
        except FileNotFoundException:
            return f"Error: File not found at {filepath}"
        filename = os.path.basename(filepath)

        from agent.bucky_client import upload_to_bucky

        url = upload_to_bucky(file_content, filename)
        return _surface(ctx.deps).post_file_link(url, filename, title=title, comment=initial_comment)
    except Exception as e:
        return f"Error uploading file: {str(e)}"


@agent.tool
def search_web_tool(ctx: RunContext[AgentDeps], query: str, num_results: int = 8) -> str:
    """Search the web using Exa. Returns results with titles, URLs, and snippets.

    Use for: current events, research, finding resources, verifying facts. NOT for reading a
    page whose URL you have (or one you can tell from a link): use fetch_url_tool for that.

    Args:
        query: The search query string.
        num_results: Number of results (1-20, default 8).
    """
    from agent.tools.web_search import search_web, specific_page_url

    page = specific_page_url(query)
    if page:
        return (f"Not searched: this query is after one specific page. Read it directly with "
                f"fetch_url_tool(url={page!r}). To search within a whole site, use site:<domain> "
                "without a path.")
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
        try:
            image_data = bytes(sandbox.files.read(image_path, format="bytes"))
        except FileNotFoundException:
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
    except FileNotFoundException:
        return ToolReturn(f"Error: no file found at {path}")
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


_VISION_GATE_ERROR = (
    "Error: computer use needs a model that can see screenshots, and this turn is "
    "running on `{model}`, which can't. Ask the user to re-send their message starting "
    "with `[!WITH:vision]` — that pins the run to a vision-capable model for this turn."
)

_SCREENSHOT_POST_MIN_INTERVAL_SECONDS = 8
_STREAM_KEEPALIVE_SECONDS = 120


def _should_force_pause_sandbox(channel_id: str, thread_ts: str) -> bool:
    """False while a run_background_command job is still pending on this
    thread — run_agent's end-of-turn cleanup otherwise force-pauses the
    sandbox unconditionally whenever anything armed a keepalive countdown
    during the turn, which would freeze a background job the instant the
    turn that started it ends, defeating the entire point of backgrounding
    it (see agent.tools.sandbox_background's module docstring)."""
    from agent.background_jobs_store import has_pending_jobs
    return not has_pending_jobs(channel_id, thread_ts)


def _maybe_post_screenshot(ctx: RunContext[AgentDeps], png: bytes) -> None:
    """Post a desktop screenshot to the thread as its own message, throttled so a fast
    screenshot/click loop (computer_use, or the model checking in on a --headed
    agent-browser session) doesn't spam the channel with one message per action.

    Best-effort: any failure here must never break the actual computer_use action's
    return value to the model, so exceptions are swallowed after a warning log.
    """
    now = time.time()
    if now - ctx.deps.last_screenshot_post_ts < _SCREENSHOT_POST_MIN_INTERVAL_SECONDS:
        return
    try:
        from agent.web64_client import upload_bytes
        url = upload_bytes(png, "screenshot.png", mime="image/png")
        _surface(ctx.deps).post_image(url, "desktop screenshot")
        ctx.deps.last_screenshot_post_ts = now
    except Exception as e:
        logger.warning(f"Failed to post desktop screenshot to thread: {e}")


@agent.tool
def computer_use(
    ctx: RunContext[AgentDeps],
    action: str,
    x: int | None = None,
    y: int | None = None,
    x2: int | None = None,
    y2: int | None = None,
    text: str = "",
    keys: str = "",
    direction: str = "down",
    amount: int = 1,
    target: str = "",
) -> ToolReturn[str]:
    """Use a real XFCE desktop (mouse, keyboard, screenshots) inside your sandbox.

    Needs a vision-capable model, see a screenshot after every action to know where
    things are and what happened. If the current turn isn't running on one, this
    returns an error telling the user to re-send with `[!WITH:vision]`.

    Call `computer_stream_tool` once, BEFORE your first action here, so the user has
    a live view instead of just a final report, easy to forget mid-task since this
    tool itself never prompts for it. Skipping it isn't fatal (screenshots still post
    to the thread as you go), but do it by default for anything that isn't a single
    trivial click.

    A "screenshot" action also posts that image to the thread itself (throttled to at
    most once every few seconds), so the user sees progress inline without needing to
    open the live stream. This works the same way during a --headed agent-browser
    session (same shared desktop), call `action="screenshot"` periodically as a
    check-in even if you don't strictly need it to decide your next move, so the user
    gets to see it happen instead of just a final report.

    Actions:
    - "screenshot": see the current screen (no other args). ALWAYS start here and take
      one after every action that might change the screen, coordinates only make sense
      relative to what you just saw.
    - "click" / "right_click" / "middle_click" / "double_click": x, y (pixel coords from
      the last screenshot). Omit x/y to click at the current cursor position.
    - "move_mouse": x, y (required).
    - "scroll": direction ("up"/"down"), amount (number of notches).
    - "drag": x, y, x2, y2 (drag from one point to another).
    - "type": text (typed at the current text cursor/focus).
    - "key": keys, a single key name ("enter", "escape", "tab") or a "+"-joined combo
      ("ctrl+c", "cmd+shift+t").
    - "wait": amount (milliseconds), for a page/app to finish loading or animating.
    - "open_url": target (a URL, opened in the default browser).
    - "launch_app": target (an app's .desktop id, e.g. "firefox-esr", "org.gnome.gedit").

    Args:
        action: One of the actions above.
        x, y: Primary coordinate (pixels, from the most recent screenshot).
        x2, y2: Second coordinate, for "drag".
        text: Text to type, for action="type".
        keys: Key name, or a "+"-joined combo (e.g. "ctrl+c"), for action="key".
        direction: Scroll direction, for action="scroll".
        amount: Scroll notches or wait milliseconds, depending on action.
        target: URL or app name, for "open_url" / "launch_app".
    """
    if not os.environ.get("E2B_API_KEY"):
        return ToolReturn("Error: E2B_API_KEY not configured.")
    if not provider_config.is_vision_model(ctx.model.model_name):
        return ToolReturn(_VISION_GATE_ERROR.format(model=ctx.model.model_name))
    # "ctrl+c" -> ["ctrl", "c"] for a combo; a bare "enter" stays a single string —
    # press_key (agent/desktop_helpers.py) already handles both, and already rejoins a
    # list with "+" for xdotool, so this is just moving the same split earlier: keys
    # used to be typed `list[str] | str`, a compound/union shape a model has to
    # correctly nest JSON for, now it's the same plain "+"-joined string a human would
    # type — see api_parameters (slack_api_call) for the same fix and why it mattered.
    parsed_keys: list[str] | str | None = None
    if keys.strip():
        parsed_keys = keys.split("+") if "+" in keys else keys
    try:
        result = _computer_use_dispatch(
            ctx.deps.channel_id, ctx.deps.thread_ts, action,
            x=x, y=y, x2=x2, y2=y2,
            text=text or None, keys=parsed_keys,
            direction=direction, amount=amount, target=target or None,
        )
    except Exception as e:
        return ToolReturn(f"Error: {e}")
    ctx.deps.keep_sandbox_warm = True
    if ctx.deps.sandbox_keepalive_seconds > 0:
        # Any action is "activity" — reset the auto-pause countdown so a stream stays
        # live through a whole click/screenshot sequence, not just the first command.
        sandbox_keepalive.arm(ctx.deps.channel_id, ctx.deps.thread_ts, ctx.deps.sandbox_keepalive_seconds)
    if isinstance(result, bytes):
        _maybe_post_screenshot(ctx, result)
        return ToolReturn(
            "Screenshot of your desktop.",
            content=[
                "(Desktop screenshot via computer_use)",
                BinaryContent(data=result, media_type="image/png", vendor_metadata={"detail": "high"}),
            ],
        )
    return ToolReturn(result)


@agent.tool
def computer_stream_tool(ctx: RunContext[AgentDeps]) -> str:
    """Start (or re-share) a live, view-only stream of your desktop and post it to the thread.

    Call this once when you begin a computer-use session so the user can watch what
    you're doing. Safe to call again later in the same session to re-post the link.
    """
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured."
    try:
        url = _computer_stream_start(ctx.deps.channel_id, ctx.deps.thread_ts)
    except Exception as e:
        return f"Error starting desktop stream: {e}"
    ctx.deps.keep_sandbox_warm = True
    ctx.deps.sandbox_keepalive_seconds = _STREAM_KEEPALIVE_SECONDS
    sandbox_keepalive.arm(ctx.deps.channel_id, ctx.deps.thread_ts, _STREAM_KEEPALIVE_SECONDS)
    error = _surface(ctx.deps).post_embed(
        url, "coolton's desktop", "coolton's desktop — live (view-only)",
    )
    if error:
        return f"{error} | url: {url}"
    return "Live desktop view posted to the thread (view-only)."


@agent.tool
def agent_browser_stream_tool(ctx: RunContext[AgentDeps]) -> str:
    """Start (or re-share) a live, view-only VNC stream and post it to the thread, the
    SAME desktop stream computer_stream_tool shows.

    Call this once before your first `agent-browser open --headed ...` in a nontrivial
    session so the user can watch a real browser window happen live, not just a final
    report. Then run agent-browser with `DISPLAY=:0 agent-browser open --headed <url>`
    (both flags required, without --headed it stays invisible even with the stream up).
    Safe to call again later to re-post the link.
    """
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured."
    try:
        url = _agent_browser_stream_start(ctx.deps.channel_id, ctx.deps.thread_ts)
    except Exception as e:
        return f"Error starting agent-browser stream: {e}"
    ctx.deps.keep_sandbox_warm = True
    ctx.deps.sandbox_keepalive_seconds = _STREAM_KEEPALIVE_SECONDS
    sandbox_keepalive.arm(ctx.deps.channel_id, ctx.deps.thread_ts, _STREAM_KEEPALIVE_SECONDS)
    error = _surface(ctx.deps).post_embed(
        url, "coolton's desktop", "coolton's desktop — live (view-only) — agent-browser renders here with --headed",
    )
    if error:
        return f"{error} | url: {url}"
    return "Live browser view posted to the thread."


_SANDBOX_KEEPALIVE_MAX_SECONDS = 1800


@agent.tool
def set_sandbox_keepalive_tool(ctx: RunContext[AgentDeps], seconds: int) -> str:
    """Manually control how long the sandbox stays up after your last action before
    auto-pausing, while a VNC stream is running.

    computer_stream_tool / agent_browser_stream_tool already set this to 120s when they
    start a stream, and every sandbox action resets the countdown, you normally don't
    need to touch this. Use it if 120s isn't enough (e.g. you expect a long gap with no
    commands in between, like waiting on the user to look at something) by raising it,
    or set it to 0 to go back to pausing immediately after each command. Clamped to
    0-1800 seconds.
    """
    seconds = max(0, min(seconds, _SANDBOX_KEEPALIVE_MAX_SECONDS))
    ctx.deps.sandbox_keepalive_seconds = seconds
    if seconds > 0:
        ctx.deps.keep_sandbox_warm = True
        sandbox_keepalive.arm(ctx.deps.channel_id, ctx.deps.thread_ts, seconds)
        return f"Sandbox keepalive set to {seconds}s — it'll stay up that long after each action before auto-pausing."
    sandbox_keepalive.cancel(ctx.deps.channel_id, ctx.deps.thread_ts)
    return "Sandbox keepalive disabled — back to pausing immediately after each command."


@agent.tool
def generate_image_tool(
    ctx: RunContext[AgentDeps],
    prompt: str,
    n: int = 1,
    size: str = "1024x1024",
    aspect_ratio: str = "",
    quality: str = "low",
    reference_images: list[str] | None = None,
) -> str:
    """Generate AI images from a text prompt, or edit existing ones.

    To EDIT or combine images ("make the sky purple", "put this logo on that
    shirt"), pass their sandbox paths as `reference_images` (up to 4, each under
    8MB; get Slack attachments into the sandbox first with
    download_attachments_to_sandbox). Editing always uses HCAI's image models.

    Tries, in order: the user's BYOK image endpoint if they have one set
    (quality has no effect on this, it's their own model, not a choice
    between ours); otherwise HCAI, using the model `quality` picks, falling
    back automatically to the OTHER quality's HCAI model if that one's
    request fails (e.g. HCAI itself is down); otherwise the global
    OPENAI_API_KEY as a last resort.

    Saves the generated image(s) into a sandbox (starting one for this thread if it doesn't
    have one yet) and returns their sandbox paths, never the raw image bytes, which would
    otherwise dump megabytes of base64 into your own context. Use upload_file_from_sandbox
    to send them to Slack.

    Args:
        prompt: Text description of the desired image.
        n: Number of images (1-4, default 1).
        size: Size ("1024x1024", "1792x1024", "1024x1792", default "1024x1024").
        aspect_ratio: Optional aspect ratio like "16:9", "1:1", "9:16", "4:3".
            Overrides size when it maps to a known size; otherwise passed through
            to providers that support an `aspect_ratio` field.
        quality: "high" (HCAI google/gemini-3-pro-image-preview, slower,
            better) or "low" (HCAI google/gemini-2.5-flash-image,
            faster, default). Only chooses between HCAI's two models; ignored
            entirely when a BYOK image endpoint is used instead.
        reference_images: Sandbox paths of images to edit/combine (optional).
    """
    quality = (quality or "low").strip().lower()
    if quality not in ("high", "low"):
        return f"Error: quality must be \"high\" or \"low\", got {quality!r}."

    from agent.tools.image_gen import edit_images, generate_image, save_images_to_sandbox

    if reference_images:
        if len(reference_images) > 4:
            return "Error: pass at most 4 reference_images."
        if not os.environ.get("E2B_API_KEY"):
            return "Error: editing images needs the sandbox (E2B_API_KEY), which isn't configured."
        try:
            sandbox, _ = get_or_create_sandbox(ctx.deps.channel_id, ctx.deps.thread_ts)
            references = [bytes(sandbox.files.read(path, format="bytes")) for path in reference_images]
        except Exception as e:
            return f"Error reading reference images from the sandbox: {_redact(str(e), context='generate_image_tool')}"
        result = edit_images(prompt, references, quality)
    else:
        result = generate_image(
            ctx.deps.user_id, prompt, n, size, aspect_ratio or None, quality,
        )
    if "image(s)" not in result:
        return result

    urls = [line.split(". ", 1)[-1] for line in result.splitlines()[1:] if line]

    if not os.environ.get("E2B_API_KEY"):
        # No sandbox capability at all in this deployment — the raw result
        # (which may be a large base64 data: URI) is the only option left.
        return result

    try:
        sandbox, _ = get_or_create_sandbox(ctx.deps.channel_id, ctx.deps.thread_ts)
    except Exception as e:
        logger.warning("generate_image_tool: couldn't get/create a sandbox: %s", e)
        return result

    saved = save_images_to_sandbox(sandbox, urls)
    if not saved:
        return result
    return (
        f"Generated {len(saved)} image(s), saved to the following files:\n"
        + "\n".join(f"- {p}" for p in saved)
    )


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
        return f"Error posting diagram: {res_json}"
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
    error = _surface(ctx.deps).post_image(url, "Mermaid diagram")
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
    # No explicit channel/thread given: summarize THIS conversation, through the
    # surface (works the same on Slack and web). An explicit channel_id/thread_ts
    # always means "summarize that other Slack thread", which is inherently
    # Slack-specific regardless of what platform is running this turn.
    if not channel_id and not thread_ts:
        return _surface(ctx.deps).summarize()
    if not channel_id:
        channel_id = ctx.deps.channel_id
    if not thread_ts:
        thread_ts = ctx.deps.thread_ts
    from agent.slack_access import channel_read_error
    denied = channel_read_error(channel_id, ctx.deps.channel_id)
    if denied:
        return f"Error: {denied}"
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
    from agent.slack_access import channel_read_error
    denied = channel_read_error(channel_id, ctx.deps.channel_id)
    if denied:
        return f"Error: {denied}"
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

    Use whenever you want what's on a particular page: the user shares a URL, a message or
    search result links one, or you need the full text of a page found via search_web.
    Always fetch a page you have the URL for, never search_web for it.

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
    """Post a message as coolton to any Slack channel, thread, or DM (a user id opens a DM).
    The message always carries a "(sent from <@user>)" footer crediting who asked.

    Use when the user explicitly asks you to post somewhere mid-turn (progress updates,
    standalone posts). For replies in the current thread, prefer the final response instead.

    Args:
        channel_id: Target channel ID (or a user ID for a DM).
        text: Message text (Markdown supported).
        thread_ts: Optional thread timestamp to post into.
    """
    from agent.attribution import attribute_text, attribution_user_id
    from agent.tools.slack_info import post_message_to_target
    credited = attribution_user_id(ctx.deps)
    return post_message_to_target(channel_id=channel_id, text=attribute_text(text, credited), thread_ts=thread_ts)


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
    return _surface(ctx.deps).remove_reaction(emoji_name, timestamp)


@agent.tool
def upload_emoji_tool(ctx: RunContext[AgentDeps], name: str, path: str = "", alias_for: str = "") -> str:
    """Add a custom Slack emoji. Pass `path` (a sandbox image file, starts a sandbox for this
    thread if it doesn't have one yet) to upload a new emoji, or `alias_for` (an existing emoji
    name) to create an alias instead. Exactly one of the two is required. Only available if
    EMOJI_PROXY_TOKEN is configured; says so plainly if it isn't.

    Args:
        name: The new emoji name, without colons (lowercase letters/numbers/dashes/underscores only).
        path: Sandbox path to the image to upload as a new emoji.
        alias_for: Name of an existing emoji to alias, instead of uploading a new image.
    """
    from agent.tools.slack_emoji import upload_emoji
    return upload_emoji(ctx.deps.channel_id, ctx.deps.thread_ts, name, path, alias_for)


@agent.tool
def submit_feedback_tool(ctx: RunContext[AgentDeps], kind: str, body: str) -> str:
    """Record feedback about coolton itself so it reaches the maintainer. Use this when someone
    reports that you're broken or wrong, praises something you did, or asks for a change or new
    capability, as a conversational alternative to the thumbs up/down buttons under a specific
    reply. Write `body` in your own words as a self-contained report: what they were doing, what
    happened, and what they expected. Do not use this for anything other than feedback about
    coolton, and do not use it as a substitute for actually answering the person.

    Args:
        kind: One of "bug", "praise", "suggestion", "other".
        body: Self-contained feedback report.
    """
    from agent.tools.feedback import submit_feedback
    return submit_feedback(ctx.deps.user_id, ctx.deps.channel_id, kind, body)


@agent.tool
def search_slack_tool(ctx: RunContext[AgentDeps], query: str, count: int = 10) -> str:
    """Search Slack messages in public channels (plus the current conversation). Your
    default message search.

    Keyword search, not natural language: a message matches only if it contains your
    words, so search for distinctive words or an exact "quoted phrase" the message would
    contain, never a whole question. Supports Slack search syntax like
    `in:#channel from:@user`. Matches in private channels or DMs other than the current
    conversation are left out.

    Args:
        query: The search query.
        count: Number of results to return (default 10, max 20).
    """
    from agent.tools.slack_search import search_slack_messages
    return search_slack_messages(query, count, current_channel_id=ctx.deps.channel_id)


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
def read_sandbox_file_tool(
    ctx: RunContext[AgentDeps], path: str, offset: int = 1, limit: int = 2000,
) -> str:
    """Read a file from the sandbox filesystem, prefer this over `cat`/`head`/
    `tail` via run_linux_command. Output is line-numbered (like `cat -n`), so
    you can reference exact lines back to the user or as context for
    edit_sandbox_file_tool.

    Args:
        path: Path to file (e.g., /home/user/file.txt or ~/attachments/data.csv).
        offset: 1-indexed line to start reading from (default 1, the top).
            Use this to page through a file larger than `limit`.
        limit: Max number of lines to return (default 2000).
    """
    from agent.tools.sandbox_files import read_sandbox_file
    return read_sandbox_file(ctx.deps.channel_id, ctx.deps.thread_ts, path, offset, limit)


@agent.tool
def write_sandbox_file_tool(ctx: RunContext[AgentDeps], path: str, content: str) -> str:
    """Write content to a file in the sandbox filesystem, OVERWRITING it
    entirely if it already exists. Creates parent dirs. Use this for a new
    file, or when you're replacing a file's content wholesale, for a
    targeted change to an existing file, use edit_sandbox_file_tool instead,
    it's cheaper and can't accidentally drop unrelated content.

    Args:
        path: Path to write (e.g., /home/user/output.txt).
        content: Text content to write.
    """
    from agent.tools.sandbox_files import write_sandbox_file
    return write_sandbox_file(ctx.deps.channel_id, ctx.deps.thread_ts, path, content)


@agent.tool
def edit_sandbox_file_tool(
    ctx: RunContext[AgentDeps], path: str, old_string: str, new_string: str, replace_all: bool = False,
) -> str:
    """Replace an exact string in an existing sandbox file, prefer this over
    `sed`/sandbox rewrites via run_linux_command for any targeted code change.

    old_string must match the file's existing content EXACTLY (whitespace and
    indentation included) and must be unique in the file unless
    replace_all=True, read the file with read_sandbox_file_tool first if
    you're not certain of its exact contents, and include enough surrounding
    lines in old_string to pin down the one occurrence you mean.

    Args:
        path: Path to the file to edit (must already exist, use
            write_sandbox_file_tool to create a new one).
        old_string: The exact text to find and replace.
        new_string: The text to replace it with.
        replace_all: Replace every occurrence instead of requiring old_string
            to be unique (default False). Use this for e.g. renaming a
            variable throughout the file.
    """
    from agent.tools.sandbox_files import edit_sandbox_file
    return edit_sandbox_file(ctx.deps.channel_id, ctx.deps.thread_ts, path, old_string, new_string, replace_all)


@agent.tool
def search_sandbox_files_tool(
    ctx: RunContext[AgentDeps],
    pattern: str,
    path: str = "/home/user",
    glob: str = "",
    case_insensitive: bool = False,
    output_mode: str = "content",
    context_lines: int = 0,
    head_limit: int = 100,
) -> str:
    """Search file CONTENTS in the sandbox with a regex, prefer this over
    `grep`/`rg` via run_linux_command for finding where something is defined
    or used. For finding files BY NAME instead, use list_sandbox_files_tool.

    Args:
        pattern: Extended regex (grep -E syntax) to search for.
        path: File or directory to search (default /home/user).
        glob: Optional filename glob to restrict the search to (e.g. "*.py").
        case_insensitive: Match case-insensitively (default False).
        output_mode: "content" (matching lines with line numbers, default),
            "files_with_matches" (just the file paths), or "count" (per-file
            match counts).
        context_lines: Lines of context to show before/after each match
            (content mode only, default 0).
        head_limit: Cap on the number of output lines returned (default 100)
            so a broad search can't flood your context, narrow `pattern`/
            `glob`/`path` instead of raising this if you hit the cap.
    """
    from agent.tools.sandbox_files import search_sandbox_files
    return search_sandbox_files(
        ctx.deps.channel_id, ctx.deps.thread_ts, pattern, path,
        glob, case_insensitive, output_mode, context_lines, head_limit,
    )


@agent.tool
def list_sandbox_files_tool(
    ctx: RunContext[AgentDeps], pattern: str = "*", path: str = "/home/user", limit: int = 200,
) -> str:
    """Find files in the sandbox BY NAME/PATTERN, prefer this over `find`/`ls`
    via run_linux_command. Supports "**" for recursive matching (e.g.
    "**/*.py" finds every .py file under `path`, at any depth). Results are
    sorted by modification time, most recently modified first. To search file
    CONTENTS instead, use search_sandbox_files_tool.

    Args:
        pattern: Glob pattern, relative to `path` unless it starts with "/"
            (default "*"). Use "**/*.ext" to search recursively.
        path: Directory to search from (default /home/user).
        limit: Max number of results to return (default 200).
    """
    from agent.tools.sandbox_files import list_sandbox_files
    return list_sandbox_files(ctx.deps.channel_id, ctx.deps.thread_ts, pattern, path, limit)


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
    thread_ts: str | None = None,
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
    if thread_ts:
        payload["thread_ts"] = thread_ts
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    try:
        response = requests.post("https://slack.com/api/chat.postMessage", json=payload, headers=headers, timeout=30)
        res_json = response.json()
        if res_json.get("ok"):
            return f"Success: Embed sent to {channel_id}"
        error = res_json.get("error", "unknown")
        metadata = res_json.get("response_metadata", {})
        return f"Error: {error} | url: {url} | metadata: {metadata}"
    except Exception as e:
        return f"Error sending web embed: {str(e)}"


_EMBED_THUMBNAIL_URL = "https://placehold.co/1280x720?text=click%20to%20open%20the%20\\ncoolton%20embed"


_WHITEBOARD_ID_RE = re.compile(r"^[0-9A-Fa-f]{6}$")


def send_whiteboard_embed(
    text: str = "whiteboard", title: str = "whiteboard", whiteboard_id: str | None = None,
) -> tuple[str, str, str, str]:
    """Build a Felix (tldraw) whiteboard's url/title/text/id. Posting it into the
    current conversation is the caller's job, via Surface.post_embed — the one
    call site that already knows how to show a live embed on Slack vs. the web
    UI, same as computer_stream_tool/agent_browser_stream_tool."""
    if whiteboard_id is None or not _WHITEBOARD_ID_RE.match(whiteboard_id):
        # whiteboard_id is model-supplied (the tool docstring invites a
        # specific id) and gets interpolated straight into a URL path that
        # WebSurface.post_embed hands the frontend as an iframe src — anything
        # other than the 6-hex-digit id it's documented to be (e.g. a path
        # traversal or query-string payload aimed at the felix host) is
        # silently replaced with a fresh random one rather than used as-is.
        whiteboard_id = f"{random.randint(0, 0xFFFFFF):06X}"
    url = f"https://whiteboard.felix.hackclub.app/{whiteboard_id}"
    return url, f"{title} #{whiteboard_id}", f"{text} #{whiteboard_id}", whiteboard_id


@agent.tool
def send_whiteboard_embed_tool(
    ctx: RunContext[AgentDeps], text: str = "whiteboard",
    title: str = "whiteboard", whiteboard_id: str | None = None,
) -> str:
    """Send a Felix whiteboard (tldraw) embed to the current thread.

    Creates a new whiteboard with a random ID at felix's tldraw instance.

    Args:
        text: Fallback text (default: "whiteboard").
        title: Embed title (default: "whiteboard").
        whiteboard_id: Optional specific 6-digit uppercase hex ID like "3A9F01" (default: random).
    """
    url, title_with_id, text_with_id, whiteboard_id = send_whiteboard_embed(
        text=text, title=title, whiteboard_id=whiteboard_id,
    )
    result = _surface(ctx.deps).post_embed(url, title_with_id, text_with_id, _EMBED_THUMBNAIL_URL)
    if result.startswith("Success"):
        return f"{result} (whiteboard id: {whiteboard_id})"
    return result


def send_html_embed(html: str, title: str = "html embed", text: str = "html embed") -> str:
    """Host HTML on the coolton file server and return its URL. Posting it into
    the current conversation is the caller's job, via Surface.post_embed."""
    from html import escape as _html_escape

    from agent.web64_client import upload_bytes
    if "<head" not in html.lower():
        meta = (
            f'<head><meta property="og:title" content="{_html_escape(title)}"/>'
            f'<meta property="og:description" content="{_html_escape(text)}"/></head>'
        )
        html = meta + html
    return upload_bytes(html.encode(), "embed.html", mime="text/html")


@agent.tool
def send_html_embed_tool(
    ctx: RunContext[AgentDeps], html: str, text: str = "html embed",
    title: str = "html embed",
) -> str:
    """Send custom HTML as a live embed in the current thread.

    Your HTML is hosted on the coolton file server (2390.proxy.tanjim.org) as a
    short URL and sent as a live embed (same mechanism as the whiteboard embed).
    There is no size limit.

    IMPORTANT: the embed's default background varies (it can be black, white, or
    the viewer's theme), so NEVER rely on default colors, always set an explicit
    background-color AND text color in the CSS (e.g. a styled <body> or <div>
    wrapper), otherwise text can be invisible (e.g. black text on a black
    background).

    Args:
        html: Raw HTML content.
        text: Fallback text (default: "html embed").
        title: Embed title (default: "html embed").
    """
    try:
        url = send_html_embed(html, title=title, text=text)
    except Exception as e:
        return f"Error hosting HTML embed: {e}"
    return _surface(ctx.deps).post_embed(url, title, text, _EMBED_THUMBNAIL_URL)


def _parse_json_object_param(param_name: str, value: str, example: str = '{"key": "value"}') -> tuple[dict | None, str | None]:
    """Parse `value` (a JSON-encoded object) into a dict. Returns (parsed, None) on
    success, or (None, error_message) on failure.

    Confirmed live: a schema-less nested `object` parameter (no defined properties,
    just `{"type": "object"}`) gives the model nothing to anchor against, and at least
    one model/provider combination reliably sent an empty `{}` for it even when it
    could correctly state in plain English what the value should contain — renaming
    the field alone didn't fix it, but switching to a plain string the model fills
    with JSON text (an ordinary language-model-native task) did. Every tool that used
    to take a schema-less `dict` parameter now takes a JSON-encoded string instead and
    parses it here.
    """
    text = (value or "").strip()
    if not text:
        return {}, None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return None, (
            f"Error: {param_name} must be valid JSON (a plain object, e.g. "
            f"'{example}') — got invalid JSON: {e}"
        )
    if not isinstance(parsed, dict):
        return None, (
            f"Error: {param_name} must be a JSON object (e.g. '{example}'), "
            f"not a {type(parsed).__name__}."
        )
    return parsed, None


def _parse_api_parameters(api_parameters: str) -> tuple[dict | None, str | None]:
    return _parse_json_object_param("api_parameters", api_parameters, example='{"channel": "C0123456"}')




def _prepare_slack_api_call(ctx: RunContext[AgentDeps], method: str, api_parameters: str) -> tuple[dict | None, str | None]:
    """Shared by slack_api_call and slack_api_call_as_bot_tool: parse the
    params, enforce the method allowlist and read rules (agent.slack_access),
    and credit any message to whoever asked (agent.attribution)."""
    parsed_parameters, parse_error = _parse_api_parameters(api_parameters)
    if parse_error:
        return None, parse_error
    from agent.attribution import MESSAGE_METHODS, attribute_api_params, attribution_user_id, is_valid_method
    from agent.slack_access import check_api_call
    if not is_valid_method(method):
        return None, f"Error: invalid Slack API method {method!r} — pass just the method name, e.g. 'chat.postMessage'."
    denied = check_api_call(method, parsed_parameters, ctx.deps.channel_id, ctx.deps.user_id)
    if denied:
        return None, denied
    if method == "chat.postMessage":
        if not parsed_parameters.get("channel"):
            return None, "Error: chat.postMessage requires a 'channel' (channel id or user id for a DM) param — use the chat_postMessage tool instead."
        if not parsed_parameters.get("text"):
            return None, "Error: chat.postMessage requires a 'text' param — use the chat_postMessage tool instead."
    credited = attribution_user_id(ctx.deps)
    if method.lower() in MESSAGE_METHODS:
        # Messages always post as coolton itself: no name or picture override, including
        # one the model passes in (which could pose as someone else).
        parsed_parameters = {
            k: v for k, v in parsed_parameters.items() if k not in ("username", "icon_url", "icon_emoji")
        }
    return attribute_api_params(method, parsed_parameters, credited), None


def _with_change_notice(ctx: RunContext[AgentDeps], method: str, params: dict, call) -> str:
    """Make one Slack API call with the channel notice naming who asked for it
    (agent.change_notices). `call(on_success)` makes the call, calls `on_success`
    with the response if it succeeds, and returns the tool result. The notice is
    posted once the call succeeds, or for NOTICE_FIRST_METHODS (archiving) before
    it, and deleted again if the call then fails."""
    from agent.attribution import attribution_user_id
    from agent.change_notices import NOTICE_FIRST_METHODS, NOTICE_METHODS, delete_notice, post_change_notice

    requester = attribution_user_id(ctx.deps)
    if method in NOTICE_FIRST_METHODS:
        posted = post_change_notice(method, params, {}, requester)
        if posted is None:
            return f"Error: couldn't post the notice naming who asked in that channel, so {method} wasn't called."
        succeeded = []
        result = call(succeeded.append)
        if not succeeded:
            delete_notice(posted)
        return result
    if method in NOTICE_METHODS:
        return call(lambda response: post_change_notice(method, params, response, requester))
    return call(None)


@agent.tool
def slack_api_call(ctx: RunContext[AgentDeps], method: str, api_parameters: str) -> str:
    """Make a Slack Web API call as cooltonUser.

    Use for Slack Web API methods not covered by other tools. Only an allowlisted set of
    methods works (reads, posting, reactions, pins, joining/leaving channels, user/team
    lookups), anything else is refused with the full list. Reading a channel other than
    the current one only works for public channels. Most methods need at least one param,
    don't guess with an empty object, check what the method actually requires first.

    Example: slack_api_call(method="conversations.join", api_parameters='{"channel": "C0123456"}')

    Args:
        method: Slack API method (e.g., 'chat.postMessage', 'conversations.info').
        api_parameters: JSON-encoded object of parameters for the method, as a plain
            STRING (e.g. '{"channel": "C0123456"}'), not a nested object.
    """
    user_token = os.environ.get("SLACK_USER_TOKEN")
    if not user_token:
        return "Error: SLACK_USER_TOKEN not configured"
    parsed_parameters, error = _prepare_slack_api_call(ctx, method, api_parameters)
    if error:
        return error
    url = f"https://slack.com/api/{method}"
    headers = {"Authorization": f"Bearer {user_token}"}
    form = {
        k: json.dumps(v) if isinstance(v, (dict, list)) else v
        for k, v in parsed_parameters.items()
    }

    def call(on_success) -> str:
        try:
            response = requests.post(url, data=form, headers=headers, timeout=30)
            res_json = _strip_secret_keys(response.json())
            if res_json.get("ok"):
                if on_success:
                    on_success(res_json)
                return f"Success: {_redact(str(res_json), context='slack_api_call')}"
            return f"Slack API error: {_redact(str(res_json), context='slack_api_call')}"
        except Exception as e:
            return f"Error: {_redact(str(e), context='slack_api_call')}"

    return _with_change_notice(ctx, method, parsed_parameters, call)


@agent.tool
def slack_api_call_as_bot_tool(ctx: RunContext[AgentDeps], method: str, api_parameters: str) -> str:
    """Make a Slack Web API call as the BOT (not cooltonUser).

    Uses SLACK_BOT_TOKEN. Use for bot-level actions like posting messages as the bot,
    updating bot messages, managing bot's own reactions, etc. Same method allowlist and
    read rules as slack_api_call. Most methods need at least one param, don't guess with
    an empty object, check what the method actually requires first.

    Example: slack_api_call_as_bot_tool(method="conversations.join", api_parameters='{"channel": "C0123456"}')

    Args:
        method: Slack API method (e.g., 'chat.postMessage', 'chat.update', 'reactions.add').
        api_parameters: JSON-encoded object of parameters for the method, as a plain
            STRING (e.g. '{"channel": "C0123456"}'), not a nested object.
    """
    parsed_parameters, error = _prepare_slack_api_call(ctx, method, api_parameters)
    if error:
        return error
    from agent.tools.slack_bot_api import slack_api_call_as_bot
    return _with_change_notice(
        ctx, method, parsed_parameters,
        lambda on_success: slack_api_call_as_bot(method, parsed_parameters, on_success=on_success),
    )


@agent.tool
def create_slack_bot_tool(ctx: RunContext[AgentDeps], manifest: str) -> str:
    """Create a Slack app from a manifest. Returns app_id and an OAuth install URL.

    Uses the xoxe config token. The manifest must include display_information.name.
    After creating, tell the user to visit the oauth_authorize_url to install the
    app, the bot token is captured and registered AUTOMATICALLY once they do (no
    one needs to dig it out of the Slack UI and hand it back). Poll
    check_bot_install_status_tool with the returned app_id until it reports
    "installed", then move on to wrangler_bot_deploy_tool. The app belongs to
    the person who asked for it: only they (or the coolton maintainer) can check,
    register tokens for, update, or deploy it afterwards.

    Args:
        manifest: JSON-encoded Slack app manifest object, as a plain STRING (with
            display_information, features, etc.), not a nested object.
    """
    parsed_manifest, parse_error = _parse_json_object_param("manifest", manifest)
    if parse_error:
        return parse_error
    from agent.tools.slack_bot_deploy import create_slack_bot
    from agent.attribution import attribution_user_id
    return create_slack_bot(parsed_manifest, owner_id=attribution_user_id(ctx.deps))


@agent.tool
def check_bot_install_status_tool(ctx: RunContext[AgentDeps], uuid: str) -> str:
    """Check whether a human has finished installing a bot created with create_slack_bot_tool.

    The install is captured automatically (see create_slack_bot_tool), call this
    periodically after sharing the oauth_authorize_url instead of asking the user
    to paste a token back. Once it reports "installed", the bot token is already
    registered and you can go straight to wrangler_bot_deploy_tool.

    Args:
        uuid: The app_id returned by create_slack_bot.
    """
    from agent.tools.slack_bot_deploy import check_bot_install_status
    from agent.attribution import attribution_user_id
    return check_bot_install_status(uuid, requester_id=attribution_user_id(ctx.deps))


@agent.tool
def register_bot_tokens_tool(ctx: RunContext[AgentDeps], uuid: str, bot_token: str, app_token: str = "", signing_secret: str = "") -> str:
    """Manually store bot tokens for a created Slack app. Only xoxb- bot tokens (and, if
    given, xapp- app tokens) are accepted.

    This is a FALLBACK, installs normally complete automatically (see
    create_slack_bot_tool / check_bot_install_status_tool). Only use this if the
    user says the automatic capture didn't work, or hands you a token unprompted.

    Args:
        uuid: The app_id returned by create_slack_bot.
        bot_token: The xoxb- bot token from the installed app. Required.
        app_token: The xapp- app-level token. Only needed for Socket Mode apps, it's
            generated manually on the app's Basic Information page, not via OAuth
            install, so most HTTP-mode Workers (deployed via wrangler_bot_deploy_tool)
            never have one. Omit it entirely for those.
        signing_secret: The signing secret from the app credentials (optional).
    """
    from agent.tools.slack_bot_deploy import register_bot_tokens
    from agent.attribution import attribution_user_id
    return register_bot_tokens(uuid, bot_token, app_token, signing_secret, requester_id=attribution_user_id(ctx.deps))


@agent.tool
def wrangler_bot_deploy_tool(ctx: RunContext[AgentDeps], uuid: str, working_dir: str, additional_flags: str = "") -> str:
    """Deploy a Slack bot Worker using wrangler inside the sandbox. Injects stored tokens, runs deploy, then deletes the secrets file.

    Args:
        uuid: The app_id from create_slack_bot.
        working_dir: Directory containing the bot code inside the sandbox.
        additional_flags: Extra flags for wrangler deploy (e.g. "--minify").
    """
    from agent.tools.slack_bot_deploy import wrangler_bot_deploy
    from agent.attribution import attribution_user_id
    return wrangler_bot_deploy(
        uuid, working_dir, ctx.deps.channel_id, ctx.deps.thread_ts, additional_flags,
        requester_id=attribution_user_id(ctx.deps),
    )


@agent.tool
def update_slack_bot_manifest_tool(ctx: RunContext[AgentDeps], uuid: str, manifest: str) -> str:
    """Update an already-created Slack app's manifest (apps.manifest.update).

    Use this once the Worker is deployed and its real URL is known, to point
    slash_commands[].url / settings.event_subscriptions.request_url at it, Slack only
    accepts an event-subscription request URL once it's live and answers the
    verification challenge, so it can't be set correctly until after deploy. The
    manifest passed here REPLACES the app's entire configuration: include every field
    (scopes, bot_user, display_information, etc.), not just the URL you're changing.

    Args:
        uuid: The app_id from create_slack_bot.
        manifest: JSON-encoded, FULL updated Slack app manifest object, as a plain
            STRING, not a nested object.
    """
    parsed_manifest, parse_error = _parse_json_object_param("manifest", manifest)
    if parse_error:
        return parse_error
    from agent.tools.slack_bot_deploy import update_slack_bot_manifest
    from agent.attribution import attribution_user_id
    return update_slack_bot_manifest(uuid, parsed_manifest, requester_id=attribution_user_id(ctx.deps))


@agent.tool_plain
def echo(text: str) -> str:
    """Return `text` exactly as given. Only for testing coolton itself (for example that
    secrets in tool input and output get redacted); don't use it otherwise.

    Args:
        text: The text to echo back.
    """
    return text


@agent.tool
def report_abuse_tool(
    ctx: RunContext[AgentDeps], category: Literal["nsfw", "spam", "vulnerability", "other"], reason: str,
) -> str:
    """Report abuse of coolton to its maintainer, who gets a DM with this message and a link to it.

    - "nsfw": the request is for sexual, explicit or NSFW-adjacent content.
    - "spam": the request uses you to spam: many or unsolicited messages, DMs or mentions to
      people or channels, or flooding a channel.
    - "vulnerability": someone found (or is probing) a security hole in coolton itself.
    - "other": something else against coolton's usage policy: asking you to do something the
      requester isn't authorized to request (using your accounts, like the coolton-agent GitHub
      account or cooltonUser, on things they don't own or can't access, impersonating someone,
      reading someone else's private messages or files), or any other clear abuse of coolton.
    For nsfw, spam and other, the request is stopped: after this, decline briefly and end your
    turn (other tool calls are refused). For vulnerability, report once and carry on with the task.

    Args:
        category: One of nsfw, spam, vulnerability, other.
        reason: One or two sentences on what the message asks for and why it's a problem.
    """
    from agent.abuse_report import report

    return report(ctx.deps, category, reason)


@agent.tool
def leave_thread_tool(ctx: RunContext[AgentDeps]) -> str:
    """Leave the current thread - ignore messages here until coolton is mentioned again.

    Use this when someone asks you to stop responding in a thread, including a message
    telling bots/AIs/agents in general to leave, or saying the thread is only for someone
    else. Then call skip() with no reply. A mid-thread mention still answers once but
    does not rejoin the thread.
    """
    return _surface(ctx.deps).set_engaged(False)


@agent.tool
def join_thread_tool(ctx: RunContext[AgentDeps]) -> str:
    """Join the current thread - respond to every message here until told to leave.

    Normally coolton only joins a thread when its starter message mentions it;
    a mid-thread mention answers once without joining. Use this when the user
    asks you to stay in (or keep responding in) this thread.
    """
    return _surface(ctx.deps).set_engaged(True)


@agent.tool
def create_code_channel_tool(ctx: RunContext[AgentDeps], name: str, task: str = "", private: bool = False) -> str:
    """Create a Slack "code channel" and move this whole conversation into it as
    its own single coolton conversation.

    Use it when the user asks for a code channel, or offer one (and create it once they
    agree) when a request grows into long, multi-step work that deserves its own space,
    like a coding project or a big investigation.

    `name` is a DISPLAY name, not a slug, write it like a sentence/title, e.g.
    "Code audit and bug detection in Coolton", never
    "code-audit-and-bug-detection-in-coolton". Spaces, uppercase letters, and
    unicode are all fine, and duplicate names are fine too, don't invent
    uniqueness suffixes.

    Slack adds you and the person who asked to the new channel, and (outside DMs) puts a
    join card on their message that people in this channel can join from. A few seconds after
    this returns, you pick up `task` there on your own, with this conversation's
    context carried over. Every message sent directly in that channel (not in a
    thread inside it) is then addressed to you and answered at channel level, as if
    the whole channel were one ongoing thread with you. A thread started inside the
    channel behaves like a normal Slack thread instead, separate conversation,
    mention required. So just tell the user you're moving the work over; don't keep
    working on `task` in the current thread once you've called this.

    Only usable on Slack, not available on the web UI.

    Args:
        name: The code channel's display name (see above, a real sentence,
            not a slug).
        task: What you'll be doing there, used to seed the handoff. Optional.
        private: Make the channel private (people join from the card) when asked to.
            Otherwise it gets this conversation's privacy: public from a public channel,
            private from anything else. It can't be made public from a private one.
    """
    surface = _surface(ctx.deps)
    if getattr(surface, "name", "slack") != "slack":
        return "Error: code channels are a Slack-only feature, not available here."
    from agent.tools.code_channel import create_code_channel
    return create_code_channel(
        ctx.deps.client, name, task, ctx.deps.user_id,
        ctx.deps.channel_id, ctx.deps.thread_ts, ctx.deps.message_ts, private=private,
    )


@agent.tool
def code_channel_view_tool(
    ctx: RunContext[AgentDeps], view_type: str, view_key: str = "", name: str = "", content: str = "",
    content_file: str = "", blocks: str = "", markdown: str = "", pr_url: str = "", base_branch: str = "",
    head_branch: str = "", access_level: str = "comment", resource_domains: str = "",
) -> str:
    """Add or update a tab (an artifact) in this code channel, shown next to the chat.
    Calling again with the same view_key updates that tab in place. Up to 5 tabs per
    channel, at most one diff.

    view_type:
    - "html": a full, self-contained HTML document (a report, a dashboard, a demo, a
      rendered page). Needs view_key and content (or content_file). Allow outside https
      origins for scripts/styles/images with resource_domains.
    - "diff": the work's changes as a unified diff (`git diff` output). Needs content (or
      content_file); base_branch and head_branch label it. One per channel.
    - "block_kit": Block Kit blocks (a JSON array as a string). Buttons and selects work:
      when someone uses one, you get a message saying what they did. Needs view_key.
    - "canvas": a plan or document as markdown, which channel members can comment on
      (access_level "comment", the default) while only you edit it. Updating an existing
      canvas tab keeps comments on unchanged sections. Needs view_key and markdown. Read
      its comments with code_channel_read_canvas_tool.
    - "pull_request": a pull request, by pr_url.

    Args:
        view_type: One of "html", "diff", "block_kit", "canvas", "pull_request".
        view_key: The tab's stable id (e.g. "plan", "reports/coverage.html"); reuse it to update.
        name: The tab's label.
        content: The HTML document or diff text.
        content_file: A sandbox file to read content from instead (e.g. a big diff or page).
        blocks: Block Kit blocks as a JSON array string, for block_kit.
        markdown: The canvas content, for canvas.
        pr_url: The pull request's https URL, for pull_request.
        base_branch: Base branch label, for diff.
        head_branch: Head branch label, for diff.
        access_level: Canvas only: "comment" (default), "read" or "write".
        resource_domains: HTML only: comma-separated https origins the page may load from.
    """
    from agent.tools.code_channel_tools import set_view

    if content_file and not content:
        try:
            with sandbox_use(ctx.deps.channel_id, ctx.deps.thread_ts) as use:
                use.sandbox, _ = get_or_create_sandbox(ctx.deps.channel_id, ctx.deps.thread_ts)
                content = use.sandbox.files.read(content_file)
        except Exception as e:
            return f"Error: couldn't read {content_file} from the sandbox: {e}"
    return set_view(ctx.deps.channel_id, view_type, view_key, name, content, blocks, markdown, pr_url,
                    base_branch, head_branch, access_level, resource_domains)


@agent.tool
def code_channel_list_views_tool(ctx: RunContext[AgentDeps]) -> str:
    """List this code channel's tabs (their view_key, view_id and version)."""
    from agent.tools.code_channel_tools import list_views

    return list_views(ctx.deps.channel_id)


@agent.tool
def code_channel_remove_view_tool(ctx: RunContext[AgentDeps], view_key: str = "", view_id: str = "") -> str:
    """Remove one of this code channel's tabs, by view_key or (for the diff tab) view_id.

    Args:
        view_key: The tab's view_key.
        view_id: The tab's view_id, from code_channel_list_views_tool.
    """
    from agent.tools.code_channel_tools import remove_view

    return remove_view(ctx.deps.channel_id, view_key, view_id)


@agent.tool
def code_channel_read_canvas_tool(ctx: RunContext[AgentDeps], view_key: str, include_resolved: bool = False) -> str:
    """Read a canvas tab in this code channel: its markdown and the comment threads people
    left on it (with the text each is anchored to). Check it before revising a plan.

    Args:
        view_key: The canvas tab's view_key.
        include_resolved: Also include resolved comment threads.
    """
    from agent.tools.code_channel_tools import read_canvas

    return read_canvas(ctx.deps.channel_id, view_key, include_resolved)


@agent.tool
def code_channel_context_bar_tool(ctx: RunContext[AgentDeps], items: str) -> str:
    """Set this code channel's context bar: up to 5 items pinned at the top (the repo, the
    branch, the PR, CI status...). Each call replaces all of them, so send the full set,
    and keep it current as things change ("PR #42 merged").

    Args:
        items: JSON array string of {"key", "label", "icon"?, "url"?}. key is a unique id
            (max 64 chars), label the text (max 128); icon is one of branch, folder,
            hierarchy, life-ring, link, globe, terminal, code, search, lock.
    """
    from agent.tools.code_channel_tools import set_context_bar

    return set_context_bar(ctx.deps.channel_id, items)


@agent.tool
def code_channel_commands_tool(ctx: RunContext[AgentDeps], commands: str) -> str:
    """Register slash commands for this code channel (like /run-tests or /create-pr), shown
    when someone types / here. When someone runs one, you get a message saying so, with
    its text. Each call replaces your whole set (max 10); send [] to clear them.

    Args:
        commands: JSON array string of {"name", "description", "argument_hint"?}. name has
            no leading slash: 1-31 lowercase letters, digits, - or _, not a built-in Slack
            command.
    """
    from agent.tools.code_channel_tools import set_commands

    return set_commands(ctx.deps.channel_id, commands)


@agent.tool
def code_channel_rename_tool(ctx: RunContext[AgentDeps], title: str) -> str:
    """Rename this code channel (its name and its session title), e.g. once the task is clearer.

    Args:
        title: The new name, a readable title (max 200 characters).
    """
    from agent.tools.code_channel_tools import rename

    return rename(ctx.deps.channel_id, title)


@agent.tool
def code_channel_archive_tool(ctx: RunContext[AgentDeps], summary: str) -> str:
    """Archive this code channel when the work is done: posts `summary` here, then archives
    the channel with it as the summary (also shared on the message the work started from).
    The history and tabs stay readable. ONLY when the person asks you to archive it or
    agrees to your suggestion; never on your own.

    Args:
        summary: A wrap-up of what was done, with links (Markdown).
    """
    from agent.tools.code_channel_tools import archive

    return archive(ctx.deps.client, ctx.deps.channel_id, summary)


@agent.tool
def send_message(ctx: RunContext[AgentDeps], text: str) -> str:
    """Send a message to the current Slack thread mid-turn. Use this to post progress updates,
    intermediate results, or messages that don't wait for the final response.

    This is for STATUS UPDATES, see the system prompt's STATUS UPDATES section for the exact
    format. Reminder since this is easy to forget mid-task: `text` MUST start with one marker
    character (→ ↺ ? ● ◐ ○ ⚠), a space, then the rest of the line in _italics_ (single
    underscores), e.g. `→ _checking the deploy logs for the last restart_`. Never send a plain,
    unmarked, non-italic line through this tool, that's for the final answer only, which
    doesn't use this tool at all.

    Args:
        text: The message content to send (Markdown supported).
    """
    try:
        _surface(ctx.deps).post_text(text)
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
        from agent.attribution import attribute_text, attribution_user_id
        credited = attribution_user_id(ctx.deps)
        kwargs = {"channel": channel, "markdown_text": attribute_text(_redact(text, context="chat_postMessage"), credited)}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = ctx.deps.client.chat_postMessage(**kwargs)
        if not resp.get("ok"):
            return f"Failed to send message: {resp}"
        return "Message sent."
    except Exception as e:
        return f"Failed to send message: {_redact(str(e), context='chat_postMessage')}"


async def text_only_response(ctx: RunContext[AgentDeps], emoji_name: str, response: str) -> str:
    """React to the user's message AND send your final reply in one call, ending your turn.

    Use this instead of add_emoji_reaction + a final text reply whenever the reply needs no
    other tools at all (chat, a quick answer from what you already know, a clarifying question)
    — it saves a whole round trip. Call it as your ONLY tool call. If you need any other tool,
    don't use this: react with add_emoji_reaction and answer normally at the end.

    It ends your turn immediately, so `response` must be your complete final answer. Never use
    it to say you're about to look something up or do something ("let me check..."): nothing
    runs after it, and the user gets a promise with no answer.

    Args:
        emoji_name: Slack emoji name without colons to react with (same rules as add_emoji_reaction).
        response: Your complete final reply, exactly as you'd otherwise write it (Markdown supported),
            in your usual style: lowercase, casual, no em dashes (see WRITING STYLE).
    """
    try:
        await add_emoji_reaction(ctx, emoji_name)
    except Exception:
        logger.exception("text_only_response: reaction failed; sending the reply anyway")
    return response


def _written_out_text_only_response(text: str) -> dict | None:
    """The arguments of a text_only_response call the model wrote out as its reply's text
    (seen live from Claude Haiku 5.5 on HCAI) instead of making it, or None."""
    import ast

    try:
        call = ast.parse(text.strip(), mode="eval").body
    except (SyntaxError, ValueError):
        return None
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            and call.func.id == "text_only_response"):
        return None
    try:
        args = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords if kw.arg}
        for positional in call.args:  # text_only_response({"emoji_name": ..., "response": ...})
            value = ast.literal_eval(positional)
            if isinstance(value, dict):
                args.update(value)
    except (ValueError, SyntaxError):
        return None
    return args if isinstance(args.get("response"), str) else None


async def plain_text_reply(ctx: RunContext[AgentDeps], text: str) -> str:
    """A plain-text final answer. One that's a text_only_response call written out as
    text is treated as that call, so its parameters never reach the user."""
    written = _written_out_text_only_response(text)
    if written is None:
        return text
    if not written.get("emoji_name"):
        return written["response"]
    return await text_only_response(ctx, str(written["emoji_name"]), written["response"])


# `text_only_response` is an output function, not a regular tool: calling it ends the
# run immediately with its return value as result.output, so run_agent_turn posts it
# exactly like a plain-text final answer. Plain text stays a valid final answer too.
OUTPUT_TYPE = [TextOutput(plain_text_reply), ToolOutput(text_only_response, name="text_only_response")]


@agent.tool
def get_datetime(ctx: RunContext[AgentDeps]) -> str:
    """Get the current date and time in UTC.

    Use whenever the answer depends on today's date or the current time ("what's the
    date", "how long until X", "is this recent", checking a timestamp or deadline),
    never assume it from your training data.
    """
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc)
    return f"{now.strftime('%A, %Y-%m-%d %H:%M:%S')} UTC (ISO 8601: {now.isoformat(timespec='seconds')})"


@agent.tool
def skip(ctx: RunContext[AgentDeps], preserve: bool = False) -> str:
    """Skip sending the final response message at the end of your turn.

    Use this when the user's request doesn't need a reply, when you've already
    responded via send_message, or when you have nothing to add.

    Args:
        preserve: False (default), this message was never really addressed
            to you (e.g. someone else's conversation). Call skip as your VERY
            FIRST tool in this case, before add_emoji_reaction or anything
            else: the whole turn is discarded, as if it had never happened.
            True, the message WAS addressed to you and you took real action
            this turn (started a background job, sent a status update via
            send_message, ...), you just have nothing more to say right now.
            That work stays in history for future turns, and the
            plan/thinking block is kept instead of deleted.
    """
    ctx.deps.should_skip = True
    ctx.deps.skip_preserve = preserve
    if preserve:
        ctx.deps.halted_messages = ctx.deps.last_attempt_messages
    raise HaltRun("skip")


@agent.tool
def wait_tool(ctx: RunContext[AgentDeps], seconds: int, reason: str) -> str:
    """Pause this conversation and automatically resume it later, without blocking. Use this for a
    one-time delay, spaced-out polling, or giving an external event (a deploy, a CI run, someone
    else's job) time to progress. NOT for a run_background_command job: you're woken automatically
    when that finishes, so just end your turn. NOT for anything recurring (use
    create_scheduled_task_tool) or a delay longer than 21600s/6h (use schedule_reminder_tool instead,
    though that only sends a static DM with no further reasoning).

    Before calling this, send a short message (via send_message) telling the user what you're waiting
    for, the typing indicator clears the moment your turn ends, so that message is the only lasting
    sign you're still on it. Call this LAST: it always ends your turn immediately, the same as `skip`,
    and you'll be woken up automatically in this same conversation once the wait is over.

    Args:
        seconds: How many seconds to wait (max 21600 = 6h).
        reason: What you're waiting for, and what to do once it resumes.
    """
    from agent.scheduler import create_wait
    result = create_wait(ctx.deps.user_id, ctx.deps.channel_id, ctx.deps.thread_ts, reason, seconds)
    if result.startswith("Error:"):
        return result
    ctx.deps.should_skip = True
    ctx.deps.skip_preserve = True
    ctx.deps.halted_messages = ctx.deps.last_attempt_messages
    raise HaltRun("wait")


_SKILL_INSTALL_MAX_DEPTH = 6
_SKILL_INSTALL_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2MB per file
_SKILL_INSTALL_MAX_TOTAL_BYTES = 20 * 1024 * 1024  # 20MB per skill


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
    # `package` names an arbitrary npm-fetched CLI + package payload; running it
    # directly on the host (the old behavior) would execute untrusted code as the
    # coolton service user, right next to .env's Slack/GitHub/E2B secrets. It runs
    # in the disposable sandbox instead, same as any other untrusted code coolton
    # touches. Nothing that ran there is trusted merely for having run: SKILL.md
    # must pass the exact same frontmatter validation create_skill enforces (see
    # _validate_skill_md), and every other file the skill shipped (references,
    # scripts, resources — most real skills have more than one file) is copied
    # over too, bounded and guarded against path traversal, but never executed
    # here — a script only ever runs later, inside a sandbox again, via
    # run_skill_script (see _run_skill_script_in_sandbox), and a resource is only
    # ever read as text into model context (read_skill_resource), never executed.
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured."
    channel_id = ctx.deps.channel_id
    thread_ts = ctx.deps.thread_ts
    cmd = f"cd /home/user && rm -rf .agents_skills_install && mkdir -p .agents_skills_install && cd .agents_skills_install && npx -y skills@latest add {shlex.quote(package)} -y"
    if skill:
        cmd += f" -s {shlex.quote(skill)}"
    try:
        with sandbox_use(channel_id, thread_ts) as use:
            sandbox, proxy_info = get_or_create_sandbox(channel_id, thread_ts)
            use.sandbox = sandbox
            result = sandbox.commands.run(cmd, timeout=180, envs=_proxy_env(proxy_info))
    except Exception as e:
        return f"Error: {str(e)}"

    if result.exit_code != 0:
        out = (result.stdout or "") + (result.stderr or "")
        return f"Failed to install skill (exit {result.exit_code}):\n{out[-1500:]}"

    from e2b import FileType

    from agent import skill_review

    # Everything is staged first and only lands in .agents/skills/ once the
    # change is approved (immediately, for the maintainer — see agent.skill_review).
    staging = skill_review.new_staging_dir(_repo_root())
    imported, rejected = [], []
    try:
        entries = sandbox.files.list("/home/user/.agents_skills_install/.agents/skills")
    except Exception:
        entries = []
    for entry in entries:
        if entry.type != FileType.DIR:
            continue
        slug = _safe_name(entry.name)
        if not slug or slug != entry.name:
            rejected.append(entry.name)
            continue
        try:
            content = sandbox.files.read(f"/home/user/.agents_skills_install/.agents/skills/{entry.name}/SKILL.md")
        except Exception:
            continue
        ok, err = _validate_skill_md(content)
        if not ok:
            rejected.append(f"{entry.name} ({err})")
            continue
        target_dir = os.path.join(staging, slug)
        if not _is_within(target_dir, staging):
            rejected.append(entry.name)
            continue
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, "SKILL.md"), "w") as f:
            f.write(content)

        skill_root = f"/home/user/.agents_skills_install/.agents/skills/{entry.name}"
        try:
            file_entries = sandbox.files.list(skill_root, depth=_SKILL_INSTALL_MAX_DEPTH)
        except Exception:
            file_entries = []
        copied_bytes = 0
        for fe in file_entries:
            if fe.type != FileType.FILE:
                continue
            rel = fe.path[len(skill_root):].lstrip("/")
            if not rel or rel == "SKILL.md":
                continue
            dest = os.path.join(target_dir, *rel.split("/"))
            if not _is_within(dest, target_dir):
                rejected.append(f"{entry.name}/{rel} (path escapes skill directory)")
                continue
            if copied_bytes >= _SKILL_INSTALL_MAX_TOTAL_BYTES:
                rejected.append(f"{entry.name}/{rel} (skill exceeds {_SKILL_INSTALL_MAX_TOTAL_BYTES}-byte total)")
                continue
            try:
                data = bytes(sandbox.files.read(fe.path, format="bytes"))
            except Exception:
                continue
            if len(data) > _SKILL_INSTALL_MAX_FILE_BYTES:
                rejected.append(f"{entry.name}/{rel} (file exceeds {_SKILL_INSTALL_MAX_FILE_BYTES}-byte limit)")
                continue
            copied_bytes += len(data)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)

        imported.append(slug)

    if not imported:
        shutil.rmtree(staging, ignore_errors=True)
        detail = f" Rejected entries: {', '.join(rejected)}." if rejected else ""
        return f"No valid skill found in '{package}' (the installer ran but produced nothing usable).{detail}"
    spec = {"op": "install", "staged_dir": staging, "slugs": imported}
    if skill_review.needs_review(ctx.deps):
        preview = "\n\n".join(_staged_skill_preview(staging, slug) for slug in imported)
        msg = skill_review.submit(spec, ctx.deps, f"install {', '.join(imported)} from `{package}`", preview)
    else:
        msg = _applied_directly(apply_skill_change(spec), ctx.deps, f"install {', '.join(imported)} from `{package}`")
    if rejected:
        msg += f" Skipped invalid entries: {', '.join(rejected)}."
    return msg


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
def huddlefm_request_control_tool(
    ctx: RunContext[AgentDeps], channel: str, permissions: str, events: str = "",
) -> str:
    """Request DJ control of a HuddleFM listening session, by DMing HuddleFM
    (coolton is allowlisted for this). Call this ONCE per session before using
    huddlefm_command_tool, see the system prompt's HUDDLEFM DJ section for the
    permission ids and full command reference.

    There is no immediate success reply: the session host gets an approval
    prompt in Slack and can take up to 5 minutes to respond (or never respond).
    This call only reports an immediate error (e.g. an invalid channel); after
    it returns with no error, tell the user you're waiting on the host and
    only try huddlefm_command_tool again once they say it's approved.

    Args:
        channel: The huddle's source channel, controls channel, or companion
            channel, required, and it must be one of those three.
        permissions: Comma-separated permission ids, e.g. "add,skip,pause,volume".
        events: Comma-separated event subscription names (optional, usually
            leave empty; coolton polls with `status` instead of subscribing).
    """
    from agent.tools.huddlefm import request_control
    return request_control(ctx.deps.client, channel, permissions, events)


@agent.tool
def huddlefm_command_tool(
    ctx: RunContext[AgentDeps], command_type: str, channel: str = "", fields: str = "",
) -> str:
    """Send a HuddleFM DJ command (after huddlefm_request_control_tool has been
    approved for the needed permission) and return its JSON reply.

    Args:
        command_type: One of: status, search, add, remove, move, clear, skip,
            previous, toggle, pause, resume, seek, volume, settings, end,
            release_control. See the system prompt's HUDDLEFM DJ section for
            which permission each needs and its extra fields.
        channel: The session's channel, only required if you hold grants on
            more than one HuddleFM session at once.
        fields: JSON object string of the command's extra fields, e.g.
            '{"query": "phonk"}' for search, '{"reference": "..."}' for add,
            '{"percent": 40}' for volume. Leave empty for commands that take
            none (status, clear, skip, previous, toggle, pause, resume, end,
            release_control).
    """
    parsed_fields, parse_error = _parse_json_object_param("fields", fields)
    if parse_error:
        return parse_error
    from agent.tools.huddlefm import send_command
    return send_command(ctx.deps.client, command_type, channel, parsed_fields)


@agent.tool
def delegate_to_subagent(
    ctx: RunContext[AgentDeps],
    target: str,
    task: str,
) -> str:
    """Hand one focused, self-contained task to a subagent and get its result back.

    For several independent tasks, use delegate_to_subagents instead: it runs them in
    parallel. Subagents:
    - "general": does the task with all of your tools (sandbox, web, Slack, files, email...).
    - "research": read-only Slack/web/canvas/docs research; returns compact sourced findings.
    - "explore": reads the sandbox workspace (files, grep, read-only commands) for context.
    - "summarizer": summarizes a transcript you include in the task.

    Args:
        target: One of "general", "research", "explore", "summarizer".
        task: A fully self-contained instruction: the subagent can't see this conversation,
            so include every id, link, file path and detail it needs, and what to return.
    """
    from agent.subagents import delegate

    return delegate(target, task, ctx.deps)


@agent.tool
def delegate_to_subagents(ctx: RunContext[AgentDeps], tasks: str) -> str:
    """Run several subagents IN PARALLEL, each on its own self-contained task, and get
    every result back at once (in the order given). Use it whenever a request splits into
    independent parts (several things to look up, several files or repos to check, several
    pieces of work that don't depend on each other): it's much faster than doing them one
    after another. Up to 6 at once. Targets are the same as delegate_to_subagent's.

    Example: delegate_to_subagents(tasks='[{"target": "research", "task": "Find ..."},
    {"target": "general", "task": "In the sandbox, ..."}]')

    Args:
        tasks: A JSON array, as a plain STRING, of {"target": ..., "task": ...} objects. Each
            task must be fully self-contained (the subagents can't see this conversation or
            each other).
    """
    from agent.subagents import delegate_many, parse_tasks

    parsed, error = parse_tasks(tasks)
    if error:
        return error
    return delegate_many(parsed, ctx.deps)


def _repo_root() -> str:
    return os.path.abspath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _skill_dirs() -> list[str]:
    root = _repo_root()
    return [os.path.join(root, "skills"), os.path.join(root, ".agents", "skills")]


def _run_skill_script_in_sandbox(script, args=None, ctx=None) -> str:
    """Execute a skill's bundled script (the `run_skill_script` tool) inside the
    thread's E2B sandbox instead of as a host subprocess.

    pydantic_ai_skills' default executor (LocalSkillScriptExecutor) runs every
    skill script as a real subprocess ON THIS HOST, with the full host
    environment merged in — every secret in os.environ (SLACK_BOT_TOKEN,
    COOLTON_GH_TOKEN, E2B_API_KEY, BYOK/MCP encryption keys, all of it).
    Scripts ship with skills from anywhere — install_skill fetches them from
    arbitrary npm packages, and several skills already checked into this repo
    (deploy-to-vercel, marimo-pair, ...) carry their own — so `run_skill_script`
    was a direct, always-available host-RCE-with-secrets primitive regardless
    of how a given skill's script got there. Route it through the same
    disposable sandbox every other untrusted-code path already uses
    (run_linux_command, code_mode, install_skill) instead.
    """
    if ctx is None or getattr(ctx, "deps", None) is None:
        return "Error: no run context available to route this into a sandbox."
    deps = ctx.deps
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured."
    if not script.uri:
        return f"Error: script '{script.name}' has no file to execute."
    try:
        with open(script.uri, "rb") as f:
            content = f.read()
    except OSError as e:
        return f"Error reading script '{script.name}': {e}"

    remote_name = os.path.basename(script.uri)
    remote_path = f"/home/user/skill_script_{remote_name}"

    # Mirrors pydantic_ai_skills' own LocalSkillScriptExecutor._build_args: every
    # script takes named (--flag value) arguments, never positional ones.
    #
    # Values are shlex-quoted below, but a flag NAME was interpolated as
    # f"--{key}" completely unquoted — args comes from the model's own
    # run_skill_script tool call, so a key like "foo; rm -rf ~" would inject
    # extra shell syntax into run_cmd. This only runs inside the disposable
    # sandbox (no host exposure, and the model already has unrestricted
    # sandbox code execution via other tools), but there's no reason for a
    # flag name to be anything other than an identifier — reject anything
    # else rather than let it become part of the shell command.
    _FLAG_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
    cmd_args: list[str] = []
    for key, value in (args or {}).items():
        if not _FLAG_NAME_RE.match(key):
            logger.warning("Skipping skill script arg with an unsafe flag name: %r", key)
            continue
        if isinstance(value, bool):
            if value:
                cmd_args.append(f"--{key}")
        elif isinstance(value, list):
            for item in value:
                cmd_args += [f"--{key}", shlex.quote(str(item))]
        elif value is not None:
            cmd_args += [f"--{key}", shlex.quote(str(value))]

    suffix = os.path.splitext(remote_name)[1].lower()
    interpreter = {".py": "python3", ".sh": "sh", ".bash": "bash"}.get(suffix)
    quoted_path = shlex.quote(remote_path)
    run_cmd = f"chmod +x {quoted_path} && "
    run_cmd += f"{interpreter} {quoted_path}" if interpreter else quoted_path
    if cmd_args:
        run_cmd += " " + " ".join(cmd_args)

    try:
        with sandbox_use(deps.channel_id, deps.thread_ts) as use:
            sandbox, proxy_info = get_or_create_sandbox(deps.channel_id, deps.thread_ts)
            use.sandbox = sandbox
            sandbox.files.write(remote_path, content)
            result = sandbox.commands.run(run_cmd, envs=_proxy_env(proxy_info), timeout=120)
    except Exception as e:
        return f"Error running skill script in sandbox: {e}"

    output = []
    if result.stdout:
        output.append(f"STDOUT:\n{result.stdout}")
    if result.stderr:
        output.append(f"STDERR:\n{result.stderr}")
    output.append(f"Exit Code: {result.exit_code}")
    return "\n\n".join(output)


def build_skills_capability():
    """The skills capability every agent that loads skills uses (the main
    agent, kevinton). Always wires run_skill_script to the sandbox executor —
    pydantic_ai_skills' default (LocalSkillScriptExecutor) would run skill
    scripts as host subprocesses with every secret in the environment."""
    from pydantic_ai_skills import CallableSkillScriptExecutor, SkillsCapability, SkillsDirectory

    skill_script_executor = CallableSkillScriptExecutor(func=_run_skill_script_in_sandbox)
    return SkillsCapability(
        directories=[SkillsDirectory(path=d, script_executor=skill_script_executor) for d in _skill_dirs()],
        auto_reload=True,
    )


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


def _staged_skill_preview(staging: str, slug: str) -> str:
    """What the reviewer sees for one staged skill: its SKILL.md and file list."""
    skill_dir = os.path.join(staging, slug)
    try:
        with open(os.path.join(skill_dir, "SKILL.md")) as f:
            skill_md = f.read()
    except OSError:
        skill_md = "(no SKILL.md)"
    files = sorted(
        os.path.relpath(os.path.join(root, name), skill_dir)
        for root, _dirs, names in os.walk(skill_dir) for name in names
    )
    return f"--- {slug} ---\nfiles: {', '.join(files)}\n\n{skill_md}"


def apply_skill_change(spec: dict) -> str:
    """Carry out a skill change — straight away for the maintainer, or once a
    proposal is approved (agent.skill_review). Re-checks everything, since a
    proposal can sit for a while and the skill dirs may have changed since."""
    op = spec.get("op")
    if op == "create":
        slug = spec["slug"]
        target = os.path.join(_repo_root(), "skills", slug)
        if not _is_within(target, os.path.join(_repo_root(), "skills")):
            return "Error: invalid skill name (must not escape the skills directory)."
        if os.path.exists(target):
            return f"Error: a skill named '{slug}' already exists at {target}."
        try:
            os.makedirs(target)
            with open(os.path.join(target, "SKILL.md"), "w") as f:
                f.write(spec["content"])
        except OSError as e:
            return f"Error creating skill: {e}"
        return f"Created skill '{slug}' at skills/{slug}/SKILL.md. It is now available via list_skills / load_skill."

    if op == "install":
        from agent import skill_review

        staging = spec["staged_dir"]
        skills_dir = os.path.join(_repo_root(), ".agents", "skills")
        installed = []
        try:
            for slug in spec["slugs"]:
                src, dst = os.path.join(staging, slug), os.path.join(skills_dir, slug)
                if not _is_within(dst, skills_dir) or not os.path.isdir(src):
                    continue
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                os.makedirs(skills_dir, exist_ok=True)
                shutil.move(src, dst)
                installed.append(slug)
        except OSError as e:
            return f"Error installing skill(s): {e}"
        finally:
            skill_review.discard({"spec": spec})
        if not installed:
            return "Error: the staged skill files are gone; nothing was installed."
        return f"Installed skill(s): {', '.join(installed)}. Available now via list_skills / load_skill."

    if op == "rename":
        src = _resolve_skill(spec["old_name"])
        if not src:
            return f"Error: skill '{spec['old_name']}' not found in any skill directory."
        new_slug = spec["new_slug"]
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
        return f"Renamed skill '{spec['old_name']}' -> '{new_slug}'."

    if op == "delete":
        src = _resolve_skill(spec["name"])
        if not src:
            return f"Error: skill '{spec['name']}' not found in any skill directory."
        try:
            shutil.rmtree(src)
        except OSError as e:
            return f"Error deleting skill: {e}"
        return f"Deleted skill '{spec['name']}' from {src}."

    return f"Error: unknown skill change {op!r}."


def _apply_or_submit_skill_change(ctx: RunContext[AgentDeps], spec: dict, description: str, preview: str = "") -> str:
    from agent import skill_review

    if skill_review.needs_review(ctx.deps):
        return skill_review.submit(spec, ctx.deps, description, preview)
    return _applied_directly(apply_skill_change(spec), ctx.deps, description)


def _applied_directly(result: str, deps, description: str) -> str:
    """Say outright that a change skipped review — without this the model,
    primed by every "goes to review unless the maintainer asked" note, told the
    maintainer their already-live skill was pending review. Also sends the
    maintainer an FYI when AUTO_ACCEPT_SKILLS is what let it through."""
    from agent import skill_review

    if result.startswith("Error"):
        return result
    skill_review.note_auto_accepted(deps, description)
    return f"{result} This is LIVE now — it did NOT go to review ({skill_review.skip_reason(deps)})."


@agent.tool
def create_skill(ctx: RunContext[AgentDeps], name: str, description: str, body: str = "") -> str:
    """Create a new custom agent skill in the repo's `skills/` directory.

    Use this when the user wants to "make a skill", "create a skill for X",
    "turn this workflow into a skill", or save a reusable playbook. This writes
    a proper SKILL.md (frontmatter + instructions) so the skill is discoverable
    via list_skills / load_skill. Unless the coolton maintainer asked for it, the
    new skill goes to the maintainer for review first and only goes live once
    approved. The result says which happened, relay that, don't guess. Do NOT use shell/CLI commands
    in the sandbox to create skills, they have no effect on the agent.

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
    return _apply_or_submit_skill_change(
        ctx, {"op": "create", "slug": slug, "content": content}, f"create skill `{slug}`", content,
    )


@agent.tool
def rename_skill(ctx: RunContext[AgentDeps], old_name: str, new_name: str) -> str:
    """Rename an existing agent skill (moves its folder and updates frontmatter name).

    Use this when the user wants to rename a skill. Operates on skills found in
    the repo's `skills/` or `.agents/skills/` directories. Unless the coolton
    maintainer asked for it, the rename waits for maintainer review (the result says
    which happened, relay that, don't guess). Do NOT use
    sandbox shell commands, they have no effect on the agent.

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
    if os.path.exists(os.path.join(os.path.dirname(src), new_slug)):
        return f"Error: a skill named '{new_slug}' already exists."
    return _apply_or_submit_skill_change(
        ctx, {"op": "rename", "old_name": old_name, "new_slug": new_slug},
        f"rename skill `{old_name}` -> `{new_slug}`",
    )


@agent.tool
def delete_skill(ctx: RunContext[AgentDeps], name: str) -> str:
    """Delete an agent skill folder entirely from disk.

    Use this when the user wants to remove/uninstall a skill. This is permanent.
    Operates on skills in the repo's `skills/` or `.agents/skills/` directories.
    Unless the coolton maintainer asked for it (the result says which happened, relay
    that, don't guess), the deletion waits for maintainer
    review. Do NOT use sandbox shell commands, they have no effect on the agent.

    Args:
        name: Skill name/folder to delete.
    """
    src = _resolve_skill(name)
    if not src:
        return f"Error: skill '{name}' not found in any skill directory."
    return _apply_or_submit_skill_change(
        ctx, {"op": "delete", "name": name}, f"delete skill `{name}` ({os.path.relpath(src, _repo_root())})",
    )


class _SkipResult:
    """Minimal run result for a turn that was halted (skip / !stop)."""

    output = ""

    def __init__(self, history=None):
        self._history = history or []

    def all_messages(self):
        return self._history


def run_agent(text, deps, message_history=None, images=None, resume_from=None):
    _user_info_cache.clear()
    deps.run_started_at = time.time()
    # Fresh per-turn checkpoint (see AgentDeps.last_attempt_messages) — a real Slack
    # turn already gets a brand-new AgentDeps() so this is already None, but reset
    # explicitly in case some other caller reuses a deps object across calls.
    deps.last_attempt_messages = None

    # Attribute the incoming message to its sender so the model can tell users apart.
    platform = deps.platform or SlackPlatform(deps.client)
    deps.request_text = deps.request_text or text or ""
    text = platform.format_user_message(text, deps)

    from listeners.actions.instructions_actions import get_user_instructions as _get_instructions
    custom_instructions = _get_instructions(deps.user_id)
    deps.custom_instructions = custom_instructions

    # The model that will actually run (cache-adjusted provider order) decides whether
    # the agent is vision-capable: attached images are passed straight to a vision model,
    # and see_image_from_sandbox is only exposed to one.
    try:
        provider_order = _resolve_provider_order(deps.user_id, tag=deps.provider_tag_filter)
        first_model = provider_order[0][1]["model"]
    except Exception:
        first_model = ""
    is_vision = provider_config.is_vision_model(first_model)

    # The system prompt (and the tool definitions) must be byte-identical for
    # EVERY request — every turn of every thread, every user — so providers can
    # cache that whole prefix once and reuse it everywhere. Anything that varies
    # by thread, sender, or turn (CURRENT CONTEXT, the sender's custom
    # instructions, message_ts, the current model) goes at the end instead: in
    # the user prompt, via `dynamic_context` below.
    full_prompt = platform.system_prompt + _current_year_note()
    # pydantic-ai only adds the system prompt to an EMPTY history, so a stored thread
    # would otherwise keep the prompt from its first turn forever, and a thread whose
    # history starts with its observation log (agent.observational_memory) would have none.
    gap_note = _resumed_after_note(message_history)
    stored_history = message_history
    message_history = _with_system_prompt(message_history, full_prompt)
    dynamic_context = platform.build_context_prompt(deps).strip() + "\n\n"
    if custom_instructions:
        dynamic_context += f"## USER'S CUSTOM INSTRUCTIONS\n{custom_instructions}\n\n"

    deps.user_token = deps.user_token or os.environ.get("SLACK_USER_TOKEN")
    from agent.tool_preload import collect_preloads
    preload_started = time.perf_counter()
    jev_picks = collect_preloads(getattr(deps, "tool_preload", None))
    # One Jev call answers both: which tool groups to load, and abuse checks (agent.abuse_report).
    deps.abuse_flags = {k.removeprefix("abuse_") for k in jev_picks if k.startswith("abuse_")}
    from agent.tool_preload import groups_for_channel, groups_from_text
    deps.preloaded_tool_groups = ({k for k in jev_picks if not k.startswith("abuse_")}
                                  | groups_from_text(deps.request_text) | groups_for_channel(deps.channel_id))
    if getattr(deps, "debug_timer", None) is not None and getattr(deps, "tool_preload", None) is not None:
        loaded = ", ".join(sorted(deps.preloaded_tool_groups)) or "nothing"
        deps.debug_timer.record("jev", "waiting on Jev tool preload", preload_started, time.perf_counter(), f"loaded {loaded}")

    toolsets = platform.toolsets(deps)

    all_tools = list(agent._function_toolset.tools.values())
    if not is_vision:
        all_tools = [t for t in all_tools if t.name != "see_image_from_sandbox"]
    # call_tool keeps its own retry budget (a wrong name or bad arguments gets retried).
    core_functions = [t if t.name == CALL_TOOL else t.function for t in all_tools if t.name not in DEFERRED_TOOLS]
    deferred_functions = [t.function for t in all_tools if t.name in DEFERRED_TOOLS]
    toolsets = [HiddenToolset(FunctionToolset(deferred_functions)), *toolsets]
    deps.hidden_toolsets = [t for t in toolsets if isinstance(t, HiddenToolset)]

    agent_dynamic = Agent(
        deps_type=AgentDeps,
        system_prompt=full_prompt,
        tools=core_functions,
        output_type=OUTPUT_TYPE,
    )

    capabilities = [_hooks, PrepareTools(disable_strict_for_all_tools)]
    # Each surface's own live-progress hooks (Slack's plan/thinking block,
    # the web UI's step spine) — Surface.build_hooks() is the one place this
    # decision lives, so it isn't re-derived here.
    surface_hooks = _surface(deps).build_hooks(deps)
    if surface_hooks is not None:
        capabilities.append(surface_hooks)

    capabilities.append(build_skills_capability())
    from agent.image_cap import cap_images
    capabilities.append(ProcessHistory(cap_images))
    capabilities.append(_checkpoint_hooks(deps))
    if getattr(deps, "debug_timer", None) is not None:
        from agent.debug_timing import build_timing_hooks
        capabilities.append(build_timing_hooks(deps.debug_timer))

    from agent.fast_mode import FAST_NOTE
    turn_context = (dynamic_context + gap_note + _abuse_check_note(deps.abuse_flags)
                    + (FAST_NOTE if deps.fast else "")
                    + platform.build_turn_context(deps, first_model, is_vision))
    text_with_turn_context = turn_context + text

    user_prompt: str | list = text_with_turn_context
    if images and is_vision:
        user_prompt = [
            text_with_turn_context,
            *[
                BinaryContent(
                    data=img["data"],
                    media_type=img["media_type"],
                    vendor_metadata={"detail": "high"},
                )
                for img in images
            ],
        ]

    # Jev's picks go in as a search_tools call right after the user's message,
    # which the run executes first — exactly what coolton's own search would
    # do, so the tool list (and the cached prefix) never changes.
    run_history, run_prompt = message_history, user_prompt
    from agent.slack_mcp_guard import GuardedSlackMCPToolset
    slack_mcp = any(isinstance(getattr(t, "wrapped", None), GuardedSlackMCPToolset) for t in toolsets)
    preload = None if resume_from else _preload_search_call(deps.preloaded_tool_groups, message_history, slack_mcp)
    if preload:
        # pydantic-ai only adds the system prompt itself when the history is empty.
        system = [SystemPromptPart(full_prompt)] if not message_history else []
        run_history = [*(message_history or []), ModelRequest(parts=[*system, UserPromptPart(user_prompt)]), *preload]
        run_prompt = None
    if resume_from:
        # Restarted mid-turn (listeners.events.turn.resume_orphaned_runs): continue from
        # this turn's checkpoint instead of starting the request over.
        run_history, run_prompt = _resume_history(resume_from, full_prompt), None

    run_kwargs = dict(
        user_prompt=run_prompt,
        deps=deps,
        message_history=run_history,
        toolsets=toolsets,
        capabilities=capabilities,
        # anthropic_*/openai_* settings are ignored by every provider that
        # doesn't recognize them (both are namespaced precisely so they can
        # always be passed together — see AnthropicModelSettings/
        # OpenAIModelSettings). Neither pydantic_ai nor the actual providers
        # coolton talks to enable caching on their own:
        #
        # - anthropic_cache/anthropic_cache_instructions/
        #   anthropic_cache_tool_definitions: pydantic_ai does not enable
        #   Anthropic prompt caching by default.
        # - openai_prompt_cache_key/openai_prompt_cache_retention: HCAI
        #   (coolton's primary configured provider, an OpenAI-compatible
        #   proxy) does NOT auto-cache on a matching prefix alone — verified
        #   live on 2026-08-23: an identical system prompt sent twice with no
        #   cache key showed cached_tokens=0 on both calls; the same two
        #   calls WITH a stable prompt_cache_key showed a 7372/7386-token
        #   cache hit (~90% cost reduction) on the second call. Without a
        #   cache key, a load-balanced backend has no way to route repeat
        #   requests back to the worker holding the cache. The key is shared
        #   by every thread (one per platform, since Slack and the web UI
        #   have different system prompts): the system prompt + tools prefix
        #   is identical everywhere (see full_prompt above), so even a brand
        #   new thread's first turn hits cache for it, and each thread's own
        #   growing history still caches on top of that on the same worker.
        #   24h retention covers realistic gaps between messages (the
        #   in-memory default is much shorter-lived).
        model_settings={
            # Think harder before acting: careful answers over fast ones, unless the user
            # sent [!FAST] (agent.fast_mode). Every chat model in providers.json accepts it
            # (checked live 2026-10-05); about 4x luna's default reasoning on a hard question.
            **({} if deps.fast else {"openai_reasoning_effort": "high"}),
            "anthropic_cache_instructions": True,
            "anthropic_cache_tool_definitions": True,
            "anthropic_cache": True,
            "openai_prompt_cache_key": f"coolton-{platform.name}",
            "openai_prompt_cache_retention": "24h",
        },
    )

    try:
        try:
            result, _provider = _run_with_provider_chain(agent_dynamic, run_kwargs, deps)
            return result
        except HaltRun as e:
            deps.should_skip = True
            deps.halt_reason = str(e)
            # !stop sets deps.halted_messages to a snapshot of everything up to
            # the halt (see plan_block.before_tool) — use that so the thread
            # doesn't lose the message that triggered this turn (and any tool
            # round-trips already completed) when the run gets cut off.
            # skip(preserve=True) sets it the same way, for the same reason
            # (real work happened this turn). Plain skip() (preserve=False,
            # the default) leaves it unset, so that path still reverts to the
            # pre-turn history — correct there, since it means the turn truly
            # never happened.
            history = deps.halted_messages if deps.halted_messages is not None else stored_history
            return _SkipResult(history)
    finally:
        # computer_use / agent_browser_stream_tool don't pause the sandbox after every
        # action (unlike run_linux_command) so a live stream survives the whole turn,
        # and run_linux_command itself skips its own pause whenever a keepalive
        # countdown is active (agent.sandbox_keepalive) — pause it once here instead,
        # however the turn ended. A turn never leaves the sandbox running past its own
        # end regardless of any pending countdown... UNLESS a run_background_command
        # job is still pending for this thread (_should_force_pause_sandbox), which
        # needs the opposite: still running *past* this turn's end is the entire
        # point (agent.background_jobs_poller notifies once it's actually done).
        if deps.keep_sandbox_warm and _should_force_pause_sandbox(deps.channel_id, deps.thread_ts):
            sandbox_keepalive.cancel(deps.channel_id, deps.thread_ts)
            pause_if_idle(deps.channel_id, deps.thread_ts)


# Tools kept out of the model's tool list: it finds them with `search_tools`
# and runs them with `call_tool` (agent.deferred_tools). Every tool definition
# is sent on every request, so the rarely-used ones here were most of a
# ~46k-token base prompt.
# Keep anything most turns need OUT of this set.
DEFERRED_TOOLS = frozenset({
    "agentmail_create_inbox", "agentmail_list_inboxes", "agentmail_list_messages",
    "agentmail_read_message", "agentmail_send_email",
    "huddlefm_request_control_tool", "huddlefm_command_tool",
    "create_slack_bot_tool", "check_bot_install_status_tool", "register_bot_tokens_tool",
    "update_slack_bot_manifest_tool", "wrangler_bot_deploy_tool",
    "create_scheduled_task_tool", "list_scheduled_tasks_tool", "pause_scheduled_task_tool",
    "resume_scheduled_task_tool", "delete_scheduled_task_tool",
    "create_code_channel_tool", "code_channel_view_tool", "code_channel_list_views_tool",
    "code_channel_remove_view_tool", "code_channel_read_canvas_tool", "code_channel_context_bar_tool",
    "code_channel_commands_tool", "code_channel_rename_tool", "code_channel_archive_tool",
    "analyze_csv_tool", "run_sql_on_csv_tool", "run_python_data_analysis_tool", "extract_tar_gz_tool",
    "install_opencode_tool", "run_opencode_tool",
    "send_html_embed_tool", "send_whiteboard_embed_tool", "render_mermaid_tool",
    "upload_emoji_tool", "submit_feedback_tool",
    "create_skill", "rename_skill", "delete_skill", "install_skill",
    "set_sandbox_keepalive_tool", "computer_stream_tool", "agent_browser_stream_tool",
    "invite_coolton_user_to_channel", "remove_reaction_tool", "leave_channel_tool",
    "slack_api_call_as_bot_tool",
})


def _preload_search_call(groups: set[str], history=None, slack_mcp: bool = False) -> list | None:
    """A pending search_tools call for the tools Jev preloaded plus, when the turn has the
    Slack MCP (`slack_mcp`), the Slack MCP tools every turn gets (agent.tool_preload), or
    None if there's nothing new to load. The run executes it before the first
    model request (exactly as if the model had searched), so MCP tools get their
    real schemas. Tools an earlier search in this thread already returned are
    skipped: their definitions are still in the history."""
    from uuid import uuid4

    from agent.deferred_tools import PRELOAD_CALL_ID_PREFIX, SEARCH_TOOL, found_in
    from agent.tool_preload import ALWAYS_PRELOADED_MCP_TOOLS, mcp_tools_for, tools_for

    already = found_in(history)
    mcp_names = mcp_tools_for(groups) | (ALWAYS_PRELOADED_MCP_TOOLS if slack_mcp else frozenset())
    names = [n for n in sorted(tools_for(groups)) + sorted(mcp_names) if n not in already]
    if not names:
        return None
    call = ToolCallPart(SEARCH_TOOL, {"queries": names}, tool_call_id=f"{PRELOAD_CALL_ID_PREFIX}{uuid4().hex[:12]}")
    return [ModelResponse(parts=[call])]


def _checkpoint_hooks(deps):
    """Save the turn's messages before every model request (agent.inflight_runs), so a
    restart mid-turn resumes from the last completed tool round."""
    from pydantic_ai.capabilities import Hooks

    from agent.inflight_runs import save_checkpoint

    hooks = Hooks()

    @hooks.on.model_request
    async def checkpoint(ctx, *, request_context, handler):
        save_checkpoint(deps.channel_id, deps.thread_ts, list(request_context.messages))
        return await handler(request_context)

    return hooks


RESTART_NOTE = (
    "[coolton restarted in the middle of this turn. Everything above happened before the restart: "
    "continue from where it left off, don't start over. A tool call that was still running at the "
    "restart didn't finish, so check its result before redoing anything with side effects.]"
)


def _resume_history(checkpoint: list, system_prompt: str) -> list:
    """An interrupted turn's checkpoint, ready to run again with no new user prompt: the
    restart note added to its last request, and the current system prompt."""
    messages = list(checkpoint)
    note = UserPromptPart(RESTART_NOTE)
    if messages and isinstance(messages[-1], ModelRequest):
        messages[-1] = replace(messages[-1], parts=[*messages[-1].parts, note])
    else:
        messages.append(ModelRequest(parts=[note]))
    return _with_system_prompt(messages, system_prompt)


def _abuse_check_note(flags: set[str]) -> str:
    """A turn-context line for abuse categories Jev flagged on this message (agent.tool_preload)."""
    from agent.abuse_report import CATEGORIES

    names = [CATEGORIES[f] for f in sorted(flags) if f in CATEGORIES]
    if not names:
        return ""
    return (
        f"[Automatic check: this message may involve {' and '.join(names)}. If it really does, call "
        "report_abuse_tool (for NSFW or spam, then decline; for a security hole in coolton, report it "
        "once and carry on). If it doesn't, ignore this note.]\n\n"
    )


def _with_system_prompt(history, system_prompt: str):
    """`history` with its system prompt replaced by the current one: any stored
    SystemPromptPart is dropped and `system_prompt` leads the first request. An empty
    history is left to pydantic-ai, which adds the system prompt itself."""
    if not history:
        return history
    stripped = []
    for message in history:
        if isinstance(message, ModelRequest):
            parts = [p for p in message.parts if not isinstance(p, SystemPromptPart)]
            if len(parts) != len(message.parts):
                message = replace(message, parts=parts)
            if not message.parts:
                continue
        stripped.append(message)
    first = stripped[0] if stripped else None
    if isinstance(first, ModelRequest):
        return [replace(first, parts=[SystemPromptPart(system_prompt), *first.parts]), *stripped[1:]]
    return [ModelRequest(parts=[SystemPromptPart(system_prompt)]), *stripped]


_RESUMED_AFTER_SECONDS = 10 * 60


def _resumed_after_note(history) -> str:
    """A turn-context line saying the conversation resumed after a pause of 10+ minutes
    (a temporal gap marker, as in Mastra's observational memory), so coolton doesn't
    treat a message from days later as part of the same moment."""
    import datetime

    latest = None
    for message in history or []:
        for obj in (message, *getattr(message, "parts", [])):
            ts = getattr(obj, "timestamp", None)
            if ts and (latest is None or ts > latest):
                latest = ts
    if latest is None:
        return ""
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=datetime.timezone.utc)
    gap = (datetime.datetime.now(datetime.timezone.utc) - latest).total_seconds()
    if gap < _RESUMED_AFTER_SECONDS:
        return ""
    minutes = int(gap // 60)
    if minutes < 120:
        ago = f"{minutes} minutes"
    elif minutes < 48 * 60:
        ago = f"{minutes // 60} hours"
    else:
        ago = f"{minutes // (24 * 60)} days"
    return f"[This conversation resumed after a {ago} pause; the last message before this one was at {latest:%Y-%m-%d %H:%M} UTC.]\n\n"


def _current_year_note() -> str:
    """The current year, at the very end of the system prompt. Models trained
    before it otherwise assume their training year and treat real search
    results as "future dates". Only the year, not the full date: the system
    prompt must stay byte-identical for the shared prompt cache, and a year
    changes that once a year instead of every day."""
    import datetime

    year = datetime.datetime.now(datetime.timezone.utc).year
    return (
        f"\n\n## CURRENT YEAR\nIt is {year}. Your training data may end earlier — dates up to "
        f"and including {year} are not \"in the future\"; trust them. For today's exact date or "
        f"time, call get_datetime.\n"
    )


def _turn_request_index(history: list) -> int | None:
    """Index of the turn's own request in a history that holds it: the last request
    carrying a user prompt and no tool results (a tool round's request, even with
    the restart note added to it, isn't one)."""
    for i in range(len(history) - 1, -1, -1):
        parts = getattr(history[i], "parts", [])
        if not isinstance(history[i], ModelRequest) or not any(isinstance(p, UserPromptPart) for p in parts):
            continue
        if not any(getattr(p, "part_kind", "") in ("tool-return", "retry-prompt") for p in parts):
            return i
    return None


def _fit_history_to_model(run_kwargs: dict, deps, prov_config: dict) -> None:
    """Record the window of the model about to be tried, and compact the
    history first if it doesn't fit that model's budget — so compaction only
    happens when a turn actually lands on a model too small for the thread,
    not pre-emptively for the smallest model anywhere in the chain (see
    agent.observational_memory)."""
    window = prov_config.get("context_window") or 0
    deps.model_context_window = window
    deps.model_compact_at = prov_config.get("compact_at") or 0
    from agent.observational_memory import request_overhead
    deps.model_request_overhead = request_overhead(prov_config.get("model") or "")
    history = run_kwargs.get("message_history")
    if not window or not history:
        return
    from agent.observational_memory import maybe_observe

    # No user_prompt means the turn's own request is already in the history (Jev's
    # preload, or a resumed turn): it must reach the model as itself, not as notes.
    keep_from = _turn_request_index(history) if run_kwargs.get("user_prompt") is None else None
    compacted = maybe_observe(history, deps, context_window=window, keep_from=keep_from,
                              compact_at=deps.model_compact_at, overhead=deps.model_request_overhead)
    if compacted is not history:
        logger.info(f"Observed history to fit {prov_config.get('model')} ({window:,}-token window) before trying it")
        run_kwargs["message_history"] = compacted


def _run_with_provider_chain(agent_dynamic, run_kwargs, deps, run_label: str | None = None):
    """Run an agent against the provider fallback chain, returning (result, provider_name).

    Shared by run_agent (main orchestrator) and subagents (research/explore/summarizer).
    Uses the global fallback cache: skips providers known to be dead and prefers the
    last-known-good provider first.

    `run_label` marks a run that isn't the turn's own model call (a subagent, which
    shares the turn's deps): it then leaves the turn's "Model: ..." display and
    deps.model_used alone — otherwise a subagent mid-turn relabels the turn's model,
    and history compaction's summarizer after the turn appended a stray model step
    to the finished web conversation, which the UI showed as a new turn still
    running — and its [!DEBUG] attempts are labelled with it.
    """
    from agent.fallback_cache import mark_alive, mark_dead, set_working_provider
    from agent.plan_block import set_model_task

    # Provider fallback order: BYOK endpoint → Anthropic → OpenAI → OpenRouter → Cerebras
    provider_order = _resolve_provider_order(deps.user_id, tag=deps.provider_tag_filter)

    # Outages of a provider's account (agent.fallback_cache.outage_kind: no credits, a
    # spending limit, a banned key...) are checked BEFORE the generic retryable/hard-error
    # logic below, deliberately overriding "429" being in retryable_errors: HCAI's
    # spending limits surface as a 429 on EVERY model routed through that one account, so
    # treating it as an ordinary rate limit meant retrying the same dead model with
    # exponential backoff (up to 5x for HCAI's configured max_retries) before even moving
    # to the next of its chat models, each repeating the same slow, guaranteed-to-fail
    # cycle. A key that's out switches the provider to its next key instead
    # (provider_config.on_outage); with none left, the whole family is skipped.

    # Families that hit a family-wide outage DURING this turn — the loop skips
    # their remaining models immediately. Families already cached as dead are
    # NOT seeded in here: _resolve_provider_order has already dropped them
    # from provider_order, except when that would leave nothing to try (e.g.
    # a forced [!WITH:tag] whose every model is in a dead family), and in that
    # case trying them anyway is the point — skipping them here too made such
    # a turn fail without a single attempt, so the family could never be seen
    # recovering.
    dead_families_this_turn: set[str] = set()

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
        # HCAI (and other OpenAI-compatible proxies) occasionally return HTTP 200 with a
        # blank/null body — pydantic then fails to validate it as a ChatCompletion (every
        # required field is None). Observed live: this used to fall through to the
        # `else: break` branch and downgrade to a worse model after a single bad
        # response, burning none of the provider's configured retries, even though a
        # plain retry of the SAME model succeeds virtually every time.
        "validation errors for chatcompletion",
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
    # Baseline to detect whether THIS call's own tool calls advanced the checkpoint
    # (see AgentDeps.last_attempt_messages) — deps is shared with callers like
    # subagents/kevinton that never wire up the hook that reassigns it, so comparing
    # by identity here (not just "is it non-None") keeps this inert for them instead
    # of picking up a stale checkpoint left over from some earlier, unrelated run.
    checkpoint_baseline = deps.last_attempt_messages
    base_model_settings = run_kwargs.get("model_settings") or {}

    for provider_name, prov_config in provider_order:
        family = provider_config.provider_family(provider_name)
        if family in dead_families_this_turn:
            logger.info(f"Skipping {provider_name}: '{family}' family marked dead this turn")
            continue
        provider_max_retries = prov_config.get("max_retries", max_retries)
        model_name = prov_config["model"]
        # Shown live, before the attempt even starts — not just after the whole
        # turn finishes (agent_dynamic.run_sync runs the entire tool-calling loop
        # synchronously, so waiting for it to return was the only signal callers
        # had before). Reused across retries of the same provider and updated
        # again if it falls back to a different one.
        if run_label is None:
            set_model_task(deps, f"{provider_name} / {model_name}")
        timer = getattr(deps, "debug_timer", None)
        for attempt in range(provider_max_retries):
            raw_response: dict = {}
            attempt_label = f"{run_label + ': ' if run_label else ''}{provider_name} / {model_name} (attempt {attempt + 1})"
            attempt_started = time.perf_counter()
            if timer and run_label is None:
                timer.current_provider = provider_name
            if run_label is None:
                _fit_history_to_model(run_kwargs, deps, prov_config)
            try:
                # Create model object if custom base_url (BYOK, HCAI)
                model_obj = None
                if prov_config.get("base_url"):
                    import httpx
                    from pydantic_ai.models.openai import OpenAIChatModel
                    from pydantic_ai.providers.openai import OpenAIProvider
                    from agent.provider_probe import _capture_raw_response
                    # Same raw-body capture as provider_probe.test_provider — a
                    # pydantic ValidationError on the response (e.g. "3 validation
                    # errors for ChatCompletion ... input_value=None") only says the
                    # SDK couldn't parse a ChatCompletion out of it, not what the
                    # endpoint actually sent back. See raw_response used below.
                    http_client = httpx.AsyncClient(
                        event_hooks={"response": [lambda r: _capture_raw_response(raw_response, r)]},
                        limits=httpx.Limits(max_keepalive_connections=0),
                    )
                    model_obj = OpenAIChatModel(
                        prov_config["model"],
                        provider=OpenAIProvider(
                            base_url=prov_config["base_url"],
                            api_key=prov_config["api_key"],
                            http_client=http_client,
                        ),
                    )

                # Set env vars for this provider
                if prov_config.get("api_key") and provider_name != "byok":
                    provider_config.apply_provider_env(provider_name, prov_config["api_key"])

                # Rate limit for Cerebras
                if "cerebras" in model_name.lower():
                    enforce_rate_limit()

                run_kwargs["model"] = model_obj if model_obj else model_name
                # MiniMax models served through OpenAI-compatible gateways (kilocode,
                # OpenRouter) have been observed live leaking a broken multi-tool-call
                # attempt as raw XML-ish text (see
                # agent.plan_block._looks_like_tool_call_leakage) instead of proper
                # structured tool_calls when they try to request more than one tool in
                # the same turn — the extra tool calls in that burst never actually
                # execute. Forcing one tool call per turn for these models avoids the
                # batching path that triggers it; the model still gets to call every
                # tool it needs, just across separate turns instead of one batch.
                settings = base_model_settings
                if "minimax" in model_name.lower():
                    settings = {**settings, "parallel_tool_calls": False}
                # A Claude model through an OpenAI-compatible gateway (HCAI, OpenRouter) is
                # only prompt-cached when the request asks for it: without this every call
                # paid full price for the whole ~60k-token prefix (checked live 2026-10-09:
                # cache_read 0 on every Claude Haiku 5.5 request). The top-level marker
                # caches up to the end of each request, so a turn's later steps reuse it all.
                if model_name.lower().startswith("anthropic/"):
                    settings = {**settings, "extra_body": {**(settings.get("extra_body") or {}),
                                                           "cache_control": {"type": "ephemeral"}}}
                run_kwargs["model_settings"] = settings
                result = agent_dynamic.run_sync(**run_kwargs)
                if timer:
                    timer.record("attempt", attempt_label, attempt_started, time.perf_counter())
                if provider_name != "byok":
                    if getattr(deps, "provider_tag_filter", None):
                        # A forced [!WITH:tag] run proves this model is up, but
                        # shouldn't make it everyone's first choice.
                        mark_alive(provider_name)
                    else:
                        set_working_provider(provider_name)
                if run_label is None:
                    deps.model_used = f"{provider_name} / {model_name}"
                return result, provider_name

            except HaltRun:
                if timer:
                    timer.record("attempt", attempt_label, attempt_started, time.perf_counter(), "halted (skip / wait / !stop)")
                raise

            except Exception as e:
                if timer:
                    timer.record(
                        "attempt", attempt_label, attempt_started, time.perf_counter(),
                        f"failed: {_redact(str(e), context='debug timing')[:150]}",
                    )
                if is_fatal_error(e):
                    logger.critical(f"Fatal error in {provider_name}: {_redact(str(e), context='provider {provider_name}')}")
                    raise
                err = _redact(str(e), context=f"provider {provider_name}")
                if raw_response.get("body") is not None:
                    raw_body = _redact(raw_response["body"], context="provider raw response")[:500]
                    err = f"raw HTTP {raw_response.get('status', '?')} body: {raw_body!r} | {err}"
                all_errors.append(f"{provider_name}: {err}")
                if deps.last_attempt_messages is not checkpoint_baseline:
                    # This attempt got far enough to actually run tool(s) — real side
                    # effects (a Slack message posted, a sandbox command run, etc.) may
                    # already have happened. Resume the next attempt from that
                    # checkpoint instead of restarting from the turn's original
                    # pre-tool-call history, which otherwise makes the fallback model
                    # act like the turn just "randomly reset" with no memory of any of
                    # it. pydantic_ai resumes cleanly from message_history alone when
                    # user_prompt is None (same mechanism thread continuation across
                    # turns already relies on) — see UserPromptNode in pydantic_ai's
                    # _agent_graph.py.
                    logger.warning(
                        f"{provider_name} failed after partial progress this turn — "
                        f"next attempt resumes from the last checkpoint instead of restarting."
                    )
                    run_kwargs["message_history"] = deps.last_attempt_messages
                    run_kwargs["user_prompt"] = None
                    checkpoint_baseline = deps.last_attempt_messages
                outage, next_key = (None, None) if provider_name == "byok" else provider_config.on_outage(
                    family, prov_config.get("api_key") or "", str(e), err)
                if outage == "next_key":
                    # This API key's account is out (no credits, a spending limit...), but the
                    # provider has another key: every model of the family uses it from now on.
                    for other_name, other_config in provider_order:
                        if provider_config.provider_family(other_name) == family:
                            other_config["api_key"] = next_key
                    logger.warning(f"{provider_name}: this {family} API key is out, switching to its next key: {err}")
                    continue
                if outage == "provider_out":
                    dead_families_this_turn.add(family)
                    from agent.hcai_status import HCAI_FAMILY, warn_hcai_outage
                    if family == HCAI_FAMILY:
                        warn_hcai_outage(deps, err)
                    logger.warning(
                        f"{provider_name} hit a family-wide outage — marked '{family}' dead, "
                        f"skipping its remaining models: {err}"
                    )
                    break  # Don't retry or try siblings in this family; move past it
                if is_hard_error(e):
                    if provider_name != "byok":
                        mark_dead(provider_name, err)
                    logger.warning(f"{provider_name} failed with a hard error (marked dead): {err}")
                    break  # Don't retry auth/config errors; skip this provider
                if is_retryable_error(e) and attempt < provider_max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"{provider_name} attempt {attempt + 1} failed with retryable error: {err}. Retrying in {delay}s...")
                    backoff_started = time.perf_counter()
                    time.sleep(delay)
                    if timer:
                        timer.record("backoff", f"before retrying {provider_name}", backoff_started, time.perf_counter())
                    continue
                else:
                    logger.warning(f"{provider_name} failed (attempt {attempt + 1}/{provider_max_retries}): {err}")
                    break  # Try next provider

        # All retries exhausted for this provider, try next provider
        logger.warning(f"Provider {provider_name} exhausted all retries, trying next provider...")

    # All providers failed
    errors_str = "\n".join(f"  - {err}" for err in all_errors)
    raise RuntimeError(f"All AI providers failed.\n{errors_str}")


def disable_strict_for_all_tools(ctx, tool_defs):
    return [replace(tool_def, strict=False) for tool_def in tool_defs]
