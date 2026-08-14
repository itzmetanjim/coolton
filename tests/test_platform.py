from types import SimpleNamespace

from agent.platform import PlatformAdapter
from agent.platforms.slack import SlackPlatform


def test_slack_adapter_preserves_user_message_shape():
    deps = SimpleNamespace(user_id="U123", channel_id="C123", thread_ts="1.2", message_ts="1.3")
    platform = SlackPlatform()
    assert isinstance(platform, PlatformAdapter)
    assert platform.format_user_message("hello", deps) == "U123 (U123):\nhello"


def test_slack_adapter_context_contains_legacy_fields():
    deps = SimpleNamespace(user_id="U123", channel_id="C123", thread_ts="1.2", message_ts="1.3", user_token=None)
    context = SlackPlatform().build_context_prompt(deps, "model", False)
    assert "channel_id: `C123`" in context
    assert "thread_ts: `1.2`" in context
    assert "Your user_id (the HUMAN who messaged you): `U123`" in context
