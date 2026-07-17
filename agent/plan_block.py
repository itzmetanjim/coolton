import inspect
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

try:
    from pydantic_ai.capabilities import Hooks
except Exception:  # pragma: no cover
    Hooks = None  # type: ignore


def _rich_text(text: str) -> dict:
    return {
        "type": "rich_text",
        "elements": [
            {
                "type": "rich_text_section",
                "elements": [{"type": "text", "text": text}],
            }
        ],
    }


def build_task_dict(
    task_id: str,
    title: str,
    status: str,
    details: str | None = None,
    output: str | None = None,
) -> dict:
    card = {
        "task_id": task_id,
        "title": title,
        "status": status,
    }
    if details:
        card["details"] = _rich_text(details)
    if output:
        card["output"] = _rich_text(output)
    return card


def build_plan_blocks(title: str, tasks: list[dict]) -> list[dict]:
    return [
        {
            "type": "plan",
            "block_id": f"plan{int(time.time() * 1000)}",
            "title": title,
            "tasks": tasks,
        }
    ]


def send_plan_message(deps) -> str | None:
    deps.plan_tasks[_DEFAULT_THINKING_ID] = {
        "task_id": _DEFAULT_THINKING_ID,
        "title": "Thinking",
        "status": "in_progress",
    }
    tasks = list(deps.plan_tasks.values())
    blocks = build_plan_blocks("Thinking...", tasks)
    try:
        resp = deps.client.chat_postMessage(
            channel=deps.channel_id,
            thread_ts=deps.thread_ts,
            blocks=blocks,
            text="Thinking...",
        )
        ts = resp.get("ts")
        if ts:
            logger.info(f"Plan message sent: {ts}")
            return ts
    except Exception as e:
        logger.warning(f"Failed to send plan message: {e}")
    return None


def _log_slack_error(where: str, e: Exception) -> None:
    code = None
    resp = getattr(e, "response", None)
    if isinstance(resp, dict):
        code = resp.get("error")
    logger.warning(f"{where}: {e} | slack_error={code}")


def update_plan_message(deps) -> None:
    if not deps.plan_ts:
        return
    tasks = list(deps.plan_tasks.values())
    blocks = build_plan_blocks("Thinking...", tasks)
    try:
        deps.client.chat_update(
            channel=deps.channel_id,
            ts=deps.plan_ts,
            blocks=blocks,
            text="Thinking...",
        )
    except Exception as e:
        _log_slack_error("Failed to update plan message", e)


def set_plan_error(deps, error_text: str) -> None:
    if not deps.plan_ts:
        return
    for task in deps.plan_tasks.values():
        if task.get("status") == "in_progress":
            task["status"] = "complete"
    err_id = _make_task_id()
    deps.plan_tasks[err_id] = {
        "task_id": err_id,
        "title": "Something went wrong",
        "status": "error",
        "output": _rich_output(error_text, 300),
    }
    blocks = build_plan_blocks("Error", list(deps.plan_tasks.values()))
    try:
        deps.client.chat_update(
            channel=deps.channel_id,
            ts=deps.plan_ts,
            blocks=blocks,
            text="Something went wrong",
        )
    except Exception as e:
        _log_slack_error("Failed to set plan error", e)

    deps.plan_ts = None


def finalize_plan_message(deps, result_text: str | None = None) -> None:
    """Mark tool tasks done and add an in-progress 'Responding' step.

    The actual answer is streamed AFTER this returns, so the final plan step
    must stay in_progress until complete_plan_message() is called.
    """
    if not deps.plan_ts:
        return
    for task in deps.plan_tasks.values():
        if task.get("status") == "in_progress":
            task["status"] = "complete"
    if deps.model_used:
        model_id = _make_task_id()
        deps.plan_tasks[model_id] = {
            "task_id": model_id,
            "title": f"Model: {deps.model_used}",
            "status": "complete",
        }
    respond_id = _make_task_id()
    deps.plan_tasks[respond_id] = {
        "task_id": respond_id,
        "title": "Responding",
        "status": "in_progress",
    }
    blocks = build_plan_blocks("Responding", list(deps.plan_tasks.values()))
    try:
        deps.client.chat_update(
            channel=deps.channel_id,
            ts=deps.plan_ts,
            blocks=blocks,
            text="Responding",
        )
    except Exception as e:
        _log_slack_error("Failed to finalize plan message", e)


def complete_plan_message(deps) -> None:
    """Flip the in-progress 'Responding' step to complete; the answer is now sent."""
    if not deps.plan_ts:
        return
    for task in deps.plan_tasks.values():
        if task.get("status") == "in_progress":
            task["status"] = "complete"
    blocks = build_plan_blocks("Done", list(deps.plan_tasks.values()))
    try:
        deps.client.chat_update(
            channel=deps.channel_id,
            ts=deps.plan_ts,
            blocks=blocks,
            text="Done",
        )
    except Exception as e:
        _log_slack_error("Failed to complete plan message", e)


TOOL_DISPLAY_NAMES = {
    "add_emoji_reaction": "Reacting to message",
    "invite_coolton_user_to_channel": "Inviting cooltonUser",
    "run_linux_command": "Running command in sandbox",
    "download_attachments_to_sandbox": "Downloading attachments",
    "upload_file_from_sandbox": "Uploading file",
    "search_web_tool": "Searching the web",
    "analyze_image_tool": "Analyzing image",
    "generate_image_tool": "Generating image",
    "render_mermaid_tool": "Rendering diagram",
    "summarize_thread_tool": "Summarizing thread",
    "list_channel_threads_tool": "Listing threads",
    "schedule_reminder_tool": "Scheduling reminder",
    "read_sandbox_file_tool": "Reading sandbox file",
    "write_sandbox_file_tool": "Writing sandbox file",
    "search_sandbox_files_tool": "Searching sandbox files",
    "list_sandbox_files_tool": "Listing sandbox files",
    "extract_tar_gz_tool": "Extracting archive",
    "analyze_csv_tool": "Analyzing CSV",
    "run_sql_on_csv_tool": "Running SQL on CSV",
    "run_python_data_analysis_tool": "Running data analysis",
    "install_opencode_tool": "Installing opencode",
    "run_opencode_tool": "Running opencode",
    "send_whiteboard_embed_tool": "Sending whiteboard",
    "send_html_embed_tool": "Sending HTML embed",
    "slack_api_call": "Calling Slack API",
    "slack_api_call_as_bot_tool": "Calling Slack API (bot)",
    "leave_thread_tool": "Leaving thread",
    "send_message": "Sending message",
}

_task_counter = 0
_DEFAULT_THINKING_ID = "task_thinking"


def _make_task_id():
    global _task_counter
    _task_counter += 1
    return f"task_{_task_counter}"


def _truncate(text: str, max_len: int = 200) -> str:
    if not text:
        return "Done"
    return text[:max_len] if len(text) > max_len else text


def _rich_output(text: str, max_len: int = 200) -> dict:
    """Wrap truncated text as a rich_text object (Slack requires `output` to be an object)."""
    return _rich_text(_truncate(text, max_len))


def _display_for_tool(tool_name: str) -> str:
    if tool_name in TOOL_DISPLAY_NAMES:
        return TOOL_DISPLAY_NAMES[tool_name]
    if tool_name.startswith("mcp_server:"):
        short = tool_name.split(":", 1)[1]
        return f"Slack: {short}"
    return tool_name.replace("_", " ").capitalize()


def build_plan_hooks():
    """Return a Hooks capability that tracks every tool call (local + MCP) in the plan.

    The hooks fire for ALL tools including MCP tools, so they replace the old
    per-function wrapping approach. They only act when `ctx.deps.plan_ts` is set.
    """
    hooks = Hooks()

    @hooks.on.before_tool_execute
    async def before_tool(ctx, *, call, tool_def, args):
        deps = ctx.deps
        if not deps.plan_ts:
            return args
        task_id = f"task_{call.tool_call_id}"
        display = _display_for_tool(call.tool_name)
        if _DEFAULT_THINKING_ID in deps.plan_tasks:
            del deps.plan_tasks[_DEFAULT_THINKING_ID]
        deps.plan_tasks[task_id] = {
            "task_id": task_id,
            "title": display,
            "status": "in_progress",
        }
        update_plan_message(deps)
        return args

    @hooks.on.after_tool_execute
    async def after_tool(ctx, *, call, tool_def, args, result):
        deps = ctx.deps
        if not deps.plan_ts:
            return result
        task_id = f"task_{call.tool_call_id}"
        task = deps.plan_tasks.get(task_id)
        if task is not None:
            task["status"] = "complete"
            task["output"] = _rich_output(str(result))
            update_plan_message(deps)
        return result

    @hooks.on.tool_execute_error
    async def on_tool_error(ctx, *, call, tool_def, args, error):
        deps = ctx.deps
        if deps.plan_ts:
            task_id = f"task_{call.tool_call_id}"
            task = deps.plan_tasks.get(task_id)
            if task is not None:
                task["status"] = "error"
                task["output"] = _rich_output(str(error))
                update_plan_message(deps)
        raise error

    return hooks
