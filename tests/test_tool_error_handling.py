from unittest.mock import Mock, patch

from agent.tools.mermaid_tool import _render_with_mermaid_ink
from agent.tools.sandbox_files import _get_sandbox


def test_mermaid_uses_get_instead_of_head():
    response = Mock(status_code=200)
    with patch("agent.tools.mermaid_tool.requests.get", return_value=response) as get:
        assert _render_with_mermaid_ink("graph TD; A-->B;", "default").startswith("https://")
    get.assert_called_once()
    assert get.call_args.kwargs["stream"] is True
    response.close.assert_called_once()


def test_sandbox_file_tools_report_connect_errors():
    with patch("agent.tools.sandbox_files.get_or_create_sandbox", side_effect=RuntimeError("sandbox expired")):
        sandbox, error = _get_sandbox("C123", "1.2")
    assert sandbox is None
    assert error == "Error: sandbox expired"
