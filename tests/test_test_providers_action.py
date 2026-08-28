from unittest.mock import Mock

from listeners.actions.test_providers import handle_test_providers


def _call(monkeypatch, order, probe_results):
    monkeypatch.setattr(
        "listeners.actions.test_providers.provider_config.build_provider_order",
        lambda user_id: order,
    )
    probe_mock = Mock(return_value=probe_results)
    monkeypatch.setattr("listeners.actions.test_providers.probe_all", probe_mock)

    ack = Mock()
    client = Mock()
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
    text = client.chat_postMessage.call_args.kwargs["text"]
    assert "HCAI GPT-5.6 Luna" in text
    assert "0.5s" in text
    assert ":white_check_mark:" in text


def test_formats_a_failure_with_error_detail_in_a_code_block(monkeypatch):
    order = [("openai", {"model": "m2", "api_key": "k"})]
    _ack, client, _probe_mock = _call(
        monkeypatch, order,
        [("openai", False, "OpenAI", 2.3, "status_code: 401")],
    )

    text = client.chat_postMessage.call_args.kwargs["text"]
    assert ":x:" in text
    assert "```" in text
    assert "status_code: 401" in text


def test_no_providers_configured_skips_probing_entirely(monkeypatch):
    monkeypatch.setattr(
        "listeners.actions.test_providers.provider_config.build_provider_order",
        lambda user_id: [],
    )
    probe_mock = Mock()
    monkeypatch.setattr("listeners.actions.test_providers.probe_all", probe_mock)

    ack = Mock()
    client = Mock()
    context = Mock()
    context.user_id = "U1"

    handle_test_providers(ack, {}, client, context)

    probe_mock.assert_not_called()
    client.chat_postMessage.assert_called_once_with(channel="U1", text="No AI providers configured.")
