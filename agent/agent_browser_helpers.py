# agent/agent_browser_helpers.py
"""agent-browser dashboard stream: expose the CLI's observability dashboard through
coolton's own web helper instead of a raw E2B sandbox host.

A local install of `agent-browser` on macOS showed its dashboard binding
127.0.0.1 only, with no --host flag or env var to change that — which would
make it unreachable via E2B's sandbox.get_host() (a port exposed from outside
the sandbox's network namespace). Live-verified against a real sandbox before
shipping this, though: on the actual Linux/E2B environment the dashboard binds
an additional E2B-internal interface too, and sandbox.get_host(DASHBOARD_PORT)
is directly reachable for both HTTP and WebSocket upgrades (checked all the
way through — GET / returns the dashboard's HTML, and a raw WebSocket
handshake against it completes). No extra forwarder needed; a plain
get_host(DASHBOARD_PORT) is what coolton_web_helper.py proxies to.

coolton_web_helper.py does the actual reverse-proxying to the public internet
(see AgentBrowserProxy there) — this module only starts the dashboard and
registers its host with it.
"""

import time

from agent.sandbox_helpers import _proxy_env

DASHBOARD_PORT = 4848


def _run(sandbox, proxy_info, cmd: str, **kwargs):
    return sandbox.commands.run(cmd, envs=_proxy_env(proxy_info), **kwargs)


def _dashboard_listening(sandbox, proxy_info) -> bool:
    # `|| true`: commands.run() raises on a non-zero exit, and grep legitimately
    # exits 1 while nothing is listening yet — not a real error.
    result = _run(sandbox, proxy_info, f"netstat -tuln | grep ':{DASHBOARD_PORT} ' || true")
    return bool(result.stdout.strip())


def ensure_dashboard(sandbox, proxy_info, timeout: int = 15) -> None:
    """Idempotent: start the agent-browser dashboard only if it isn't already
    running.

    Safe to call before every stream request — a resumed sandbox may have lost
    it (E2B pause/resume, sandbox recycling).
    """
    if _dashboard_listening(sandbox, proxy_info):
        return
    _run(sandbox, proxy_info, f"agent-browser dashboard start --port {DASHBOARD_PORT}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _dashboard_listening(sandbox, proxy_info):
            return
        time.sleep(0.5)
    raise RuntimeError("agent-browser dashboard did not come up")


def register_stream(sandbox, proxy_info) -> str:
    """Ensure the dashboard is up, register its public host with
    coolton_web_helper, and return the URL to embed."""
    ensure_dashboard(sandbox, proxy_info)
    upstream = sandbox.get_host(DASHBOARD_PORT)
    from agent.web64_client import register_agent_browser_stream
    return register_agent_browser_stream(upstream)
