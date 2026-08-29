"""agent-browser live view tool: start (or re-share) the dashboard stream.

Pure dispatch logic lives here; the `@agent.tool` wrapper in agent/agent.py handles
RunContext plumbing and marking deps.keep_sandbox_warm — matching how every other
tool in this codebase splits "tools/*.py logic" from "agent.py wrapper".
"""

from agent import agent_browser_helpers as ab
from agent.sandbox_helpers import get_or_create_sandbox


def agent_browser_stream(channel_id: str, thread_ts: str) -> str:
    """Start (or reuse) agent-browser's dashboard stream, returning a shareable URL."""
    sandbox, proxy_info = get_or_create_sandbox(channel_id, thread_ts)
    return ab.register_stream(sandbox, proxy_info)
