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
