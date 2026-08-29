"""agent-browser live view tool: share the SAME desktop stream computer_use uses.

agent-browser is headless by default (fast CDP automation, no real rendering needed).
Passing --headed makes it open a real Chrome window instead — and with DISPLAY set to
the sandbox's existing Xvfb/XFCE session (agent/desktop_helpers.py), that window
renders straight into the same desktop computer_stream_tool already exposes via noVNC.
No separate proxy/dashboard needed: this tool just ensures that desktop + its stream
are up, exactly like computer_stream_tool.

Pure dispatch logic lives here; the `@agent.tool` wrapper in agent/agent.py handles
RunContext plumbing and marking deps.keep_sandbox_warm — matching how every other
tool in this codebase splits "tools/*.py logic" from "agent.py wrapper".
"""

from agent import desktop_helpers as dh
from agent.sandbox_helpers import get_or_create_sandbox


def agent_browser_stream(channel_id: str, thread_ts: str) -> str:
    """Start (or reuse) the desktop's VNC stream so a --headed agent-browser session
    is visible, returning a shareable view-only URL."""
    sandbox, proxy_info = get_or_create_sandbox(channel_id, thread_ts)
    dh.ensure_desktop(sandbox, proxy_info)
    return dh.start_stream(sandbox, proxy_info)
