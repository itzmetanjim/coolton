from dataclasses import dataclass, field

from slack_sdk import WebClient


@dataclass
class AgentDeps:
    client: WebClient
    user_id: str
    channel_id: str
    thread_ts: str
    message_ts: str
    # Platform adapter is optional for backwards-compatible Slack callers.
    platform: object | None = None
    # Current-conversation transport (agent.surface.Surface) — how tools talk about
    # THIS thread (post text/images/files, react, show progress), as opposed to
    # deps.client, which every genuinely-Slack tool (posting to another channel,
    # Slack search, the Slack MCP toolset) keeps using directly. None for every
    # caller that predates this (kevinton, subagents, the scheduler, summarize_thread's
    # direct call) — get_surface() lazily builds a SlackSurface from deps.client for
    # them, so they need zero changes.
    surface: object | None = None
    user_token: str | None = None
    custom_instructions: str = ""
    plan_ts: str | None = None
    plan_tasks: dict = field(default_factory=dict)
    should_skip: bool = False
    halt_reason: str = ""
    model_used: str = ""
    run_started_at: float = 0.0
    # Set from a `[!WITH:tag]` directive in the user's message (see
    # agent/provider_config.extract_tag_directive) — forces the provider
    # fallback chain to only try models carrying this tag for the turn.
    provider_tag_filter: str | None = None
    # Snapshot of the in-progress message history, captured right before a
    # `!stop` halts the run (see plan_block.before_tool). Lets run_agent keep
    # everything up to the halt (the user's message, any completed tool
    # round-trips) instead of reverting the thread to its pre-turn state.
    halted_messages: list | None = None
    # Set whenever something needs run_agent's finally block to pause the sandbox for
    # it at the end of the turn, rather than pausing immediately after its own call:
    # computer_use never pauses per-action (a live stream needs the desktop to survive
    # between actions), and run_linux_command skips its own immediate pause whenever
    # sandbox_keepalive_seconds > 0 (see agent.sandbox_keepalive) in favor of a
    # countdown-based auto-pause instead.
    keep_sandbox_warm: bool = False
    # How long (seconds) the sandbox stays up after the last action before
    # agent.sandbox_keepalive auto-pauses it, while a VNC stream is being watched —
    # 0 (the default, reset at the start of every turn) means "pause immediately after
    # each command", the normal/cheap behavior. computer_stream_tool and
    # agent_browser_stream_tool set this to 120 when they start a stream; the model can
    # override it via set_sandbox_keepalive_tool.
    sandbox_keepalive_seconds: float = 0.0
    # Last time (time.time()) a desktop screenshot was posted to the thread as its
    # own message (agent.agent._maybe_post_screenshot). Throttles computer_use's
    # "screenshot" action so a fast click/screenshot loop doesn't spam the channel.
    last_screenshot_post_ts: float = 0.0
    # Progressive checkpoint of "everything safe to resume from" for the CURRENT
    # provider-chain attempt (agent.agent._run_with_provider_chain) — reassigned (a new
    # list, never mutated in place) on every tool call by agent.plan_block's
    # before_tool_execute hook via _messages_safe_for_resume. If one provider fails
    # mid-turn AFTER real tool calls already ran (Slack messages posted, sandbox
    # commands run, etc.), the next fallback attempt resumes from here instead of
    # silently restarting from the turn's original pre-tool-call history — which
    # otherwise looks to the user like the agent "randomly reset" mid-turn, since the
    # new model has no memory of what the previous one already did. None (the default,
    # reset at the start of every run_agent() call) means no progress has been
    # checkpointed yet. Subagents and kevinton never wire up build_plan_hooks(), so this
    # field is naturally inert (never reassigned) for their own _run_with_provider_chain
    # calls even though they share this same AgentDeps type.
    last_attempt_messages: list | None = None

    def get_surface(self):
        """The current-conversation Surface for this turn — see agent.surface.get_surface."""
        from agent.surface import get_surface as _get_surface
        return _get_surface(self)
