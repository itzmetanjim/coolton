from unittest.mock import Mock

from agent.tools.mermaid_tool import render_mermaid


def _get_ok(monkeypatch):
    def fake_get(url, stream=False, timeout=10):
        return Mock(status_code=200)

    monkeypatch.setattr("agent.tools.mermaid_tool.requests.get", fake_get)


def test_empty_diagram_error():
    assert render_mermaid("   ") == "Error: Empty diagram code"


def test_kroki_success(monkeypatch):
    _get_ok(monkeypatch)
    url = render_mermaid("graph TD; A-->B;")
    assert url.startswith("https://kroki.io/mermaid/png/")
    assert "theme" not in url


def test_kroki_dark_theme(monkeypatch):
    _get_ok(monkeypatch)
    url = render_mermaid("graph TD; A-->B;", theme="dark")
    assert url.startswith("https://kroki.io/mermaid/png/")
    assert "theme=dark" in url


def test_mermaid_ink_fallback(monkeypatch):
    calls = []

    def fake_get(url, stream=False, timeout=10):
        calls.append(url)
        if "kroki.io" in url:
            return Mock(status_code=500)
        return Mock(status_code=200)

    monkeypatch.setattr("agent.tools.mermaid_tool.requests.get", fake_get)
    url = render_mermaid("graph TD; A-->B;")
    assert url.startswith("https://mermaid.ink/img/")
    assert "theme=default" in url


def test_both_renderers_fail(monkeypatch):
    def fake_get(url, stream=False, timeout=10):
        return Mock(status_code=500)

    monkeypatch.setattr("agent.tools.mermaid_tool.requests.get", fake_get)
    result = render_mermaid("graph TD; A-->B;")
    assert result == "Error: Failed to render diagram on both kroki.io and mermaid.ink"


def test_network_error_falls_through_to_mermaid_ink(monkeypatch):
    calls = []

    def fake_get(url, stream=False, timeout=10):
        calls.append(url)
        if "kroki.io" in url:
            raise OSError("connection refused")
        return Mock(status_code=200)

    monkeypatch.setattr("agent.tools.mermaid_tool.requests.get", fake_get)
    assert render_mermaid("graph TD; A-->B;").startswith("https://mermaid.ink/")
