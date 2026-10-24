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


HEADLESS_NOTE = (
    " Note: an agent-browser browser is already running headless, so it's invisible on the "
    "desktop and in the stream. Run `agent-browser close` (with the same AGENT_BROWSER_SESSION), "
    "then open the page again: from now on browsers open as real windows on the desktop."
)


def headless_note(channel_id: str, thread_ts: str) -> str:
    """HEADLESS_NOTE if a headless agent-browser browser is running in this thread's sandbox."""
    try:
        sandbox, proxy_info = get_or_create_sandbox(channel_id, thread_ts)
        return HEADLESS_NOTE if dh.headless_browser_running(sandbox, proxy_info) else ""
    except Exception:
        return ""
