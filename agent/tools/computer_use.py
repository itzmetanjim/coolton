"""Computer-use tool: drive an XFCE desktop inside the thread's existing E2B sandbox.

Pure dispatch logic lives here; the `@agent.tool` wrapper in agent/agent.py handles
RunContext plumbing, the vision-model gate, and marking deps.keep_sandbox_warm — matching
how every other tool in this codebase splits "tools/*.py logic" from "agent.py wrapper".
"""

from agent import desktop_helpers as dh
from agent.sandbox_helpers import get_or_create_sandbox

VALID_ACTIONS = (
    "screenshot", "click", "right_click", "middle_click", "double_click",
    "move_mouse", "scroll", "drag", "type", "key", "wait", "open_url", "launch_app",
)


def computer_use(
    channel_id: str,
    thread_ts: str,
    action: str,
    x: int | None = None,
    y: int | None = None,
    x2: int | None = None,
    y2: int | None = None,
    text: str | None = None,
    keys: list[str] | str | None = None,
    direction: str = "down",
    amount: int = 1,
    target: str | None = None,
):
    """Dispatch one computer-use action against the thread's sandbox desktop.

    Returns raw PNG bytes for action="screenshot" (the caller wraps those as a real
    image ToolReturn), or a short status string for every other action.
    """
    if action not in VALID_ACTIONS:
        return f"Error: unknown action '{action}'. Valid actions: {', '.join(VALID_ACTIONS)}"

    sandbox, proxy_info = get_or_create_sandbox(channel_id, thread_ts)

    if action == "screenshot":
        return dh.screenshot(sandbox, proxy_info)
    if action == "click":
        dh.click(sandbox, proxy_info, button="left", x=x, y=y)
        return f"Clicked at ({x}, {y})" if x is not None else "Clicked"
    if action == "right_click":
        dh.click(sandbox, proxy_info, button="right", x=x, y=y)
        return f"Right-clicked at ({x}, {y})" if x is not None else "Right-clicked"
    if action == "middle_click":
        dh.click(sandbox, proxy_info, button="middle", x=x, y=y)
        return f"Middle-clicked at ({x}, {y})" if x is not None else "Middle-clicked"
    if action == "double_click":
        dh.double_click(sandbox, proxy_info, x=x, y=y)
        return f"Double-clicked at ({x}, {y})" if x is not None else "Double-clicked"
    if action == "move_mouse":
        if x is None or y is None:
            return "Error: move_mouse requires x and y."
        dh.move_mouse(sandbox, proxy_info, x, y)
        return f"Moved mouse to ({x}, {y})"
    if action == "scroll":
        dh.scroll(sandbox, proxy_info, direction=direction, amount=amount)
        return f"Scrolled {direction} x{amount}"
    if action == "drag":
        if None in (x, y, x2, y2):
            return "Error: drag requires x, y, x2, y2."
        dh.drag(sandbox, proxy_info, (x, y), (x2, y2))
        return f"Dragged from ({x}, {y}) to ({x2}, {y2})"
    if action == "type":
        if not text:
            return "Error: type requires text."
        dh.type_text(sandbox, proxy_info, text)
        return f"Typed {len(text)} characters"
    if action == "key":
        if not keys:
            return "Error: key requires keys (e.g. 'enter' or ['ctrl', 'c'])."
        dh.press_key(sandbox, proxy_info, keys)
        return f"Pressed {keys}"
    if action == "wait":
        ms = amount if amount else 500
        dh.wait_ms(sandbox, proxy_info, ms)
        return f"Waited {ms}ms"
    if action == "open_url":
        if not target:
            return "Error: open_url requires target (a URL)."
        dh.open_uri(sandbox, proxy_info, target)
        return f"Opened {target}"
    if action == "launch_app":
        if not target:
            return "Error: launch_app requires target (an app name, e.g. 'firefox-esr')."
        dh.launch_app(sandbox, proxy_info, target)
        return f"Launched {target}"

    return f"Error: action '{action}' not implemented"


def computer_stream(channel_id: str, thread_ts: str) -> str:
    """Start (or reuse) the desktop's VNC stream, returning a shareable view-only URL."""
    sandbox, proxy_info = get_or_create_sandbox(channel_id, thread_ts)
    return dh.start_stream(sandbox, proxy_info)
