from dataclasses import dataclass, field

from slack_sdk import WebClient


@dataclass
class AgentDeps:
    client: WebClient
    user_id: str
    channel_id: str
    thread_ts: str
    message_ts: str
    user_token: str | None = None
    custom_instructions: str = ""
    plan_ts: str | None = None
    plan_tasks: dict = field(default_factory=dict)
    should_skip: bool = False
