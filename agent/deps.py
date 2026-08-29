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
    # Set by any tool that starts a live view the user is watching (the CUA
    # desktop's noVNC stream, agent-browser's dashboard) this turn. run_agent
    # only pauses the sandbox (agent/desktop_helpers.py) once at the very end
    # of the turn when this is set, instead of after every single action like
    # run_linux_command does — a live stream would otherwise die mid-session.
    keep_sandbox_warm: bool = False
