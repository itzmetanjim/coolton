"""agent.langfuse_tracing: off without keys, and secrets never leave in a span."""
from types import SimpleNamespace

from agent import langfuse_tracing


def test_tracing_is_off_and_harmless_without_keys(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    langfuse_tracing.setup()
    assert not langfuse_tracing.enabled()
    with langfuse_tracing.run(name="coolton turn", user_id="U1", channel_id="C1", thread_ts="1.1"):
        pass
    langfuse_tracing.flush()


def test_known_secrets_are_masked_out_of_spans(monkeypatch):
    from langfuse.types import OtelSpanIdentifier

    monkeypatch.setattr("agent.redact.secret_values", lambda: [("GITHUB_TOKEN", "ghp_supersecret123")])
    notified = []
    monkeypatch.setattr("agent.redact._notifier", lambda hits, context: notified.append(hits))
    leaky = OtelSpanIdentifier(trace_id=1, span_id=2)
    clean = OtelSpanIdentifier(trace_id=1, span_id=3)
    params = SimpleNamespace(spans={
        leaky: SimpleNamespace(attributes={"gen_ai.input.messages": "use ghp_supersecret123 to push", "gen_ai.usage.input_tokens": 12}),
        clean: SimpleNamespace(attributes={"gen_ai.input.messages": "hi"}),
    })
    result = langfuse_tracing.mask_secrets(params=params)
    assert set(result.span_patches) == {leaky}
    assert result.span_patches[leaky].set_attributes == {"gen_ai.input.messages": "use *** to push"}
    assert notified == []  # once per span would flood the admin's DMs
