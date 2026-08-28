from unittest.mock import Mock

from listeners.actions.test_providers import handle_test_providers

_HEADER_TS = "1787900000.000100"


def _call(monkeypatch, order, probe_results):
    monkeypatch.setattr(
        "listeners.actions.test_providers.provider_config.build_provider_order",
        lambda user_id: order,
    )
    probe_mock = Mock(return_value=probe_results)
    monkeypatch.setattr("listeners.actions.test_providers.probe_all", probe_mock)

    ack = Mock()
    client = Mock()
    client.chat_postMessage.return_value = {"ts": _HEADER_TS}
    context = Mock()
    context.user_id = "U1"

    handle_test_providers(ack, {}, client, context)
    return ack, client, probe_mock


def test_uses_probe_all_instead_of_probing_sequentially(monkeypatch):
    """The button used to call test_provider in a plain for-loop, one model at
    a time — with 40+ configured models this made "Test Providers" take
    minutes. It must go through probe_all (parallel across providers) instead."""
    order = [("hcai_0", {"model": "m1", "api_key": "k"})]
    ack, client, probe_mock = _call(
        monkeypatch, order,
        [("hcai_0", True, "HCAI GPT-5.6 Luna", 0.5, "ok")],
    )

    probe_mock.assert_called_once_with(order)
    ack.assert_called_once()
    # An ephemeral message can't anchor a thread — the header must be a real post.
    client.chat_postEphemeral.assert_not_called()

    calls = client.chat_postMessage.call_args_list
    # header (no thread_ts) + notice + one results chunk, all threaded off the header.
    assert calls[0].kwargs.get("thread_ts") is None
    assert calls[0].kwargs["text"] == "Testing all AI providers..."
    for call in calls[1:]:
        assert call.kwargs["thread_ts"] == _HEADER_TS
    assert calls[1].kwargs["text"] == "(this may take a minute)"

    results_text = calls[-1].kwargs["text"]
    assert "HCAI GPT-5.6 Luna" in results_text
    assert "0.5s" in results_text
    assert ":white_check_mark:" in results_text


def test_formats_a_failure_with_error_detail_in_a_code_block(monkeypatch):
    order = [("openai", {"model": "m2", "api_key": "k"})]
    _ack, client, _probe_mock = _call(
        monkeypatch, order,
        [("openai", False, "OpenAI", 2.3, "status_code: 401")],
    )

    results_text = client.chat_postMessage.call_args_list[-1].kwargs["text"]
    assert ":x:" in results_text
    assert "```" in results_text
    assert "status_code: 401" in results_text


def test_no_providers_configured_skips_probing_but_still_replies_in_thread(monkeypatch):
    monkeypatch.setattr(
        "listeners.actions.test_providers.provider_config.build_provider_order",
        lambda user_id: [],
    )
    probe_mock = Mock()
    monkeypatch.setattr("listeners.actions.test_providers.probe_all", probe_mock)

    ack = Mock()
    client = Mock()
    client.chat_postMessage.return_value = {"ts": _HEADER_TS}
    context = Mock()
    context.user_id = "U1"

    handle_test_providers(ack, {}, client, context)

    probe_mock.assert_not_called()
    last_call = client.chat_postMessage.call_args_list[-1]
    assert last_call.kwargs["text"] == "No AI providers configured."
    assert last_call.kwargs["thread_ts"] == _HEADER_TS


def test_long_results_are_chunked_not_posted_as_one_oversized_message(monkeypatch):
    """A single un-chunked chat.postMessage over Slack's per-message char limit
    is what produced several disconnected top-level posts in production
    (looked like the button had spawned separate conversations) instead of
    replies in one thread. Many models with long error bodies must come back
    as multiple threaded chunks, not one giant message."""
    order = [(f"kilocode_{i}", {"model": f"m{i}", "api_key": "k"}) for i in range(50)]
    long_error = "x" * 2000
    probe_results = [
        (f"kilocode_{i}", False, f"Model {i}", 1.0, long_error) for i in range(50)
    ]
    _ack, client, _probe_mock = _call(monkeypatch, order, probe_results)

    calls = client.chat_postMessage.call_args_list
    result_calls = calls[2:]  # after header + notice
    assert len(result_calls) > 1  # actually split into multiple messages
    for call in result_calls:
        assert call.kwargs["thread_ts"] == _HEADER_TS
        assert len(call.kwargs["text"]) <= 38000
