import logging
import time

from agent.redact import redact as _redact
from agent.steering_store import clear_steering_messages, peek_steering_messages
from agent.stop_store import HaltRun, stop_requested_for

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


def _retrying(call, where: str, attempts: int = 3) -> None:
    """Retry a terminal plan-block Slack call a couple of times before giving up.

    Only used for the ONE-SHOT transitions (finalize/complete/error/delete) —
    those never get called again, so if the call that flips the block out of
    "in_progress" (or removes it) silently fails — a transient network blip,
    or Slack briefly rate-limiting chat.update after the burst of live
    updates a busy run produces — nothing else retries it and the "Thinking…"
    spinner is stuck showing in_progress forever. The frequent mid-run
    update_plan_message() calls don't need this: a missed one just means the
    next hook's update catches the display up.
    """
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            call()
            return
        except Exception as e:
            last_exc = e
            if attempt < attempts - 1:
                delay = 1.0 * (attempt + 1)
                resp = getattr(e, "response", None)
                headers = getattr(resp, "headers", None) if resp is not None else None
                if headers:
                    try:
                        delay = min(float(headers.get("Retry-After", delay)), 5.0)
                    except (TypeError, ValueError):
                        pass
                time.sleep(delay)
    if last_exc is not None:
        _log_slack_error(where, last_exc)


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


def set_model_task(deps, model_used: str, status: str = "in_progress") -> None:
    """Show which model is currently being tried, live — as soon as an attempt
    starts, not just after the whole turn finishes (agent_dynamic.run_sync runs
    the entire tool-calling loop synchronously, so waiting for it to return was
    the only signal previously available). Reuses one fixed task id so a
    provider fallback updates this same line instead of stacking a new one each
    time, and keeps it first in the list (see build_plan_blocks ordering).
    """
    if not deps.plan_ts:
        return
    deps.plan_tasks.pop(_MODEL_TASK_ID, None)
    task = {"task_id": _MODEL_TASK_ID, "title": f"Model: {model_used}", "status": status}
    deps.plan_tasks = {_MODEL_TASK_ID: task, **deps.plan_tasks}
    update_plan_message(deps)


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
        "output": _rich_output(_redact(error_text, context="plan error"), 300),
    }
    blocks = build_plan_blocks("Error", list(deps.plan_tasks.values()))
    _retrying(
        lambda: deps.client.chat_update(
            channel=deps.channel_id, ts=deps.plan_ts, blocks=blocks, text="Something went wrong",
        ),
        "Failed to set plan error",
    )

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
    if deps.model_used and _MODEL_TASK_ID not in deps.plan_tasks:
        # Fallback for a caller that never went through the live per-attempt
        # update (see set_model_task) — still show the model, just late. The
        # normal path already has this task (and just got flipped to
        # "complete" above), so this doesn't stack a second model line.
        model_task = {
            "task_id": _MODEL_TASK_ID,
            "title": f"Model: {deps.model_used}",
            "status": "complete",
        }
        deps.plan_tasks = {_MODEL_TASK_ID: model_task, **deps.plan_tasks}
    respond_id = _make_task_id()
    deps.plan_tasks[respond_id] = {
        "task_id": respond_id,
        "title": "Responding",
        "status": "in_progress",
    }
    blocks = build_plan_blocks("Responding", list(deps.plan_tasks.values()))
    _retrying(
        lambda: deps.client.chat_update(
            channel=deps.channel_id, ts=deps.plan_ts, blocks=blocks, text="Responding",
        ),
        "Failed to finalize plan message",
    )


def complete_plan_message(deps) -> None:
    """Flip the in-progress 'Responding' step to complete; the answer is now sent."""
    if not deps.plan_ts:
        return
    for task in deps.plan_tasks.values():
        if task.get("status") == "in_progress":
            task["status"] = "complete"
    blocks = build_plan_blocks("Done", list(deps.plan_tasks.values()))
    _retrying(
        lambda: deps.client.chat_update(
            channel=deps.channel_id, ts=deps.plan_ts, blocks=blocks, text="Done",
        ),
        "Failed to complete plan message",
    )


def delete_plan_message(deps) -> None:
    """Delete the plan/thinking message entirely (used when the model skips the turn).

    Prevents the thinking block from being left stuck 'in_progress' when the
    turn is halted without a final answer. A !stop halt keeps the block instead
    (set_plan_error), so the user can see the turn was manually stopped.
    """
    if not deps.plan_ts:
        return
    _retrying(
        lambda: deps.client.chat_delete(channel=deps.channel_id, ts=deps.plan_ts),
        "Failed to delete plan message",
    )
    deps.plan_ts = None


TOOL_DISPLAY_NAMES = {
    "add_emoji_reaction": "Reacting to message",
    "invite_coolton_user_to_channel": "Inviting cooltonUser",
    "run_linux_command": "Running command in sandbox",
    "download_attachments_to_sandbox": "Downloading attachments",
    "get_slack_file_tool": "Downloading Slack file",
    "upload_file_from_sandbox": "Uploading file",
    "search_web_tool": "Searching the web",
    "analyze_image_tool": "Analyzing image",
    "see_image_from_sandbox": "Viewing sandbox image",
    "computer_use": "Using the computer",
    "computer_stream_tool": "Sharing desktop view",
    "generate_image_tool": "Generating image",
    "render_mermaid_tool": "Rendering diagram",
    "summarize_thread_tool": "Summarizing thread",
    "list_channel_threads_tool": "Listing threads",
    "schedule_reminder_tool": "Scheduling reminder",
    "create_scheduled_task_tool": "Creating scheduled task",
    "list_scheduled_tasks_tool": "Listing scheduled tasks",
    "pause_scheduled_task_tool": "Pausing scheduled task",
    "resume_scheduled_task_tool": "Resuming scheduled task",
    "delete_scheduled_task_tool": "Deleting scheduled task",
    "fetch_url_tool": "Fetching URL",
    "get_user_tool": "Looking up user",
    "get_channel_info_tool": "Looking up channel",
    "post_message_tool": "Posting message",
    "leave_channel_tool": "Leaving channel",
    "remove_reaction_tool": "Removing reaction",
    "search_slack_tool": "Searching Slack",
    "read_conversation_history_tool": "Reading conversation history",
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
    "join_thread_tool": "Joining thread",
    "send_message": "Sending message",
    "agentmail_create_inbox": "Creating AgentMail inbox",
    "agentmail_list_inboxes": "Listing AgentMail inboxes",
    "agentmail_list_messages": "Listing AgentMail messages",
    "agentmail_read_message": "Reading AgentMail message",
    "agentmail_send_email": "Sending AgentMail email",
    "delegate_to_subagent": "Running focused subagent",
}

_task_counter = 0
_DEFAULT_THINKING_ID = "task_thinking"
_MODEL_TASK_ID = "task_model"


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


def _messages_safe_for_resume(messages: list) -> list:
    """Trim a mid-run message list down to something safe to hand back as
    `message_history` for the NEXT turn.

    `ctx.messages` at `before_tool_execute` time still ends with the
    ModelResponse that's about to be executed — its ToolCallPart(s) have no
    matching ToolReturnPart yet. Sending that dangling tool call back to the
    model API on the next turn breaks it (every tool_use needs a paired
    tool_result), so drop that trailing response; everything before it
    (including the user's message that started this turn, and any earlier
    tool round-trips this same turn already completed) is complete and safe
    to keep.
    """
    if messages and getattr(messages[-1], "parts", None) and any(
        getattr(part, "part_kind", None) == "tool-call" for part in messages[-1].parts
    ):
        return list(messages[:-1])
    return list(messages)


def build_plan_hooks():
    """Return a Hooks capability that tracks every tool call (local + MCP) in the plan.

    The hooks fire for ALL tools including MCP tools, so they replace the old
    per-function wrapping approach. They only act when `ctx.deps.plan_ts` is set.
    """
    hooks = Hooks()

    @hooks.on.after_model_request
    async def after_model(ctx, *, request_context, response):
        deps = ctx.deps
        # A response that still has tool calls attached isn't the final answer —
        # any text alongside those calls is the model's own mid-turn status
        # narration (see the STATUS UPDATES system prompt section), so post it
        # as a real thread message right away instead of holding it until the
        # turn ends. A text-only response (no tool calls) is always the final
        # answer, which run_agent_turn posts separately — never repost that here.
        has_tool_calls = any(
            getattr(part, "part_kind", None) == "tool-call" for part in response.parts
        )
        if has_tool_calls:
            status_text = "\n\n".join(
                part.content
                for part in response.parts
                if getattr(part, "part_kind", None) == "text" and getattr(part, "content", None)
            ).strip()
            if status_text:
                redacted = _redact(status_text, context="status update")
                logger.info("STATUS      | %s", _truncate(redacted, 500))
                try:
                    deps.client.chat_postMessage(
                        channel=deps.channel_id,
                        thread_ts=deps.thread_ts,
                        markdown_text=redacted,
                    )
                except Exception as e:
                    _log_slack_error("Failed to post status update", e)

        if not deps.plan_ts:
            return response
        thinking_text = "\n\n".join(
            part.content
            for part in response.parts
            if getattr(part, "part_kind", None) == "thinking" and getattr(part, "content", None)
        )
        if not thinking_text.strip():
            return response
        logger.info(
            "REASONING   | %s",
            _truncate(_redact(thinking_text, context="model reasoning"), 1000),
        )
        if _DEFAULT_THINKING_ID in deps.plan_tasks:
            del deps.plan_tasks[_DEFAULT_THINKING_ID]
        task_id = _make_task_id()
        deps.plan_tasks[task_id] = {
            "task_id": task_id,
            "title": "Reasoning",
            "status": "complete",
            "output": _rich_output(_redact(thinking_text, context="model reasoning"), 1500),
        }
        update_plan_message(deps)
        return response

    @hooks.on.before_tool_execute
    async def before_tool(ctx, *, call, tool_def, args):
        deps = ctx.deps
        logger.info(
            "TOOL INPUT  | %s | %s",
            call.tool_name,
            _truncate(_redact(_pretty_args(args), context=f"tool input {call.tool_name}"), 1000),
        )
        # If the user sent !stop in this thread after this run started, halt before the next tool.
        if stop_requested_for(deps.channel_id, deps.thread_ts, deps.run_started_at):
            deps.halted_messages = _messages_safe_for_resume(ctx.messages)
            raise HaltRun("!stop requested")
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
            "input": _truncate(_redact(_pretty_args(args), context=f"tool input {call.tool_name}"), 500),
        }
        update_plan_message(deps)
        return args

    @hooks.on.after_tool_execute
    async def after_tool(ctx, *, call, tool_def, args, result):
        deps = ctx.deps
        logger.info(
            "TOOL OUTPUT | %s | %s",
            call.tool_name,
            _truncate(_redact(_pretty_args(result), context=f"tool output {call.tool_name}"), 1000),
        )

        # A message sent into this thread while the run was already in flight
        # (see listeners/events/message.py and app_mentioned.py) queues here
        # instead of racing a whole separate turn. Fold it into the next tool
        # result the model sees so it can steer without needing a new run —
        # only clear the queue once it's actually embedded in a string result;
        # a non-string result (e.g. structured MCP output) leaves it queued
        # for the next tool call that can carry it.
        steering = peek_steering_messages(deps.channel_id, deps.thread_ts)
        if steering and isinstance(result, str):
            notes = "\n\n".join(
                "[New message from the user while you were working on this — "
                "read it and factor it in now]: " + _redact(s["text"], context="steering message")
                for s in steering
            )
            logger.info("STEERING    | %s", _truncate(notes, 500))
            result = f"{result}\n\n{notes}"
            clear_steering_messages(deps.channel_id, deps.thread_ts)

        if not deps.plan_ts:
            return result
        task_id = f"task_{call.tool_call_id}"
        task = deps.plan_tasks.get(task_id)
        if task is not None:
            task["status"] = "complete"
            task["output"] = _combined_io(
                task.get("input", ""), _redact(_safe_str(result), context=f"tool output {call.tool_name}")
            )
            update_plan_message(deps)
        return result

    @hooks.on.tool_execute_error
    async def on_tool_error(ctx, *, call, tool_def, args, error):
        deps = ctx.deps
        logger.error(
            "TOOL ERROR  | %s | %s",
            call.tool_name,
            _truncate(_redact(str(error), context=f"tool error {call.tool_name}"), 1000),
        )
        if deps.plan_ts:
            task_id = f"task_{call.tool_call_id}"
            task = deps.plan_tasks.get(task_id)
            if task is not None:
                task["status"] = "error"
                task["output"] = _combined_io(
                    task.get("input", ""), _redact(str(error), context=f"tool error {call.tool_name}")
                )
                update_plan_message(deps)
        raise error

    return hooks


def _combined_io(tool_input: str, tool_output: str) -> dict:
    """Render the tool input (command) and output together in the plan card.

    Slack plan tasks only render `title` + `output`, so the command is shown as
    a `$ command` line followed by the result.
    """
    parts = []
    if tool_input and tool_input != "Done":
        parts.append(f"$ {tool_input}")
    parts.append(_truncate(tool_output, 800) if tool_output else "Done")
    return _rich_text("\n".join(parts))


def _safe_str(value) -> str:
    """str(value), but summarize binary payloads instead of stringifying them.

    A `computer_use` screenshot (or see_image_from_sandbox) result is a
    ToolReturn whose `content` holds a BinaryContent with the raw image bytes.
    str()-ing that embeds the full byte payload as escaped text — for a
    ~200KB screenshot that's roughly a megabyte of string to build, then
    `_redact()` does a linear substring scan per known secret over the whole
    thing before any of it ever gets truncated for display. That's real,
    observed latency on every single screenshot step (visible as coolton
    "getting stuck" right after a computer_use call), not just wasted memory.
    """
    data = getattr(value, "data", None)
    if isinstance(data, (bytes, bytearray)):
        media_type = getattr(value, "media_type", "binary")
        return f"<{media_type}, {len(data)} bytes>"
    # ToolReturn-like: summarize return_value + content instead of the raw repr,
    # since content may itself hold BinaryContent.
    return_value = getattr(value, "return_value", None)
    content = getattr(value, "content", None)
    if return_value is not None or content is not None:
        pieces = [str(return_value)] if return_value is not None else []
        if isinstance(content, (list, tuple)):
            pieces.extend(_safe_str(c) for c in content)
        elif content is not None:
            pieces.append(_safe_str(content))
        return " | ".join(pieces)
    return str(value)


def _pretty_args(value) -> str:
    """Render tool args / results compactly for logging (dicts, lists, strings)."""
    if isinstance(value, dict):
        parts = []
        for k, v in value.items():
            v_str = _truncate(_safe_str(v), 300)
            parts.append(f"{k}={v_str}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_truncate(_safe_str(v), 300) for v in value) + "]"
    return _safe_str(value)
