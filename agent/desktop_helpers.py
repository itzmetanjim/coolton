# agent/desktop_helpers.py
"""Computer-use command layer: an XFCE desktop inside coolton's existing per-thread
E2B sandbox (see agent/sandbox_helpers.py).

This intentionally does NOT depend on the `e2b-desktop` package. That SDK's
`Sandbox.create()` does all of its X server / xfce4 setup at create time and never
restores state on `connect()` — exactly backwards for coolton, which reconnects to a
stored sandbox id on every tool call and may resume long after the X server died (E2B
pause/resume, sandbox recycling). Instead we port its command layer (same xdotool/scrot/
x11vnc commands, verified against e2b-dev/E2B's desktop-python package) as plain shell
commands run through the sandbox coolton already has, wrapped in an idempotent
`ensure_desktop()` that any action can call safely.

DISPLAY is never inherited — every command must pass `envs=_desktop_env(proxy_info)`.
"""

import re
import shlex
import secrets
import string
import time
from uuid import uuid4

from agent.sandbox_helpers import _proxy_env

DISPLAY = ":0"
_VNC_PORT = 5900
_NOVNC_PORT = 6080

# xdotool keysym names for common human-friendly key names. Matches e2b-desktop's map so
# behavior (e.g. what `["ctrl", "c"]` presses) is unsurprising to anyone who's used it.
KEYS = {
    "alt": "Alt_L",
    "alt_left": "Alt_L",
    "alt_right": "Alt_R",
    "backspace": "BackSpace",
    "break": "Pause",
    "caps_lock": "Caps_Lock",
    "cmd": "Super_L",
    "command": "Super_L",
    "control": "Control_L",
    "control_left": "Control_L",
    "control_right": "Control_R",
    "ctrl": "Control_L",
    "del": "Delete",
    "delete": "Delete",
    "down": "Down",
    "end": "End",
    "enter": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "f1": "F1", "f2": "F2", "f3": "F3", "f4": "F4", "f5": "F5", "f6": "F6",
    "f7": "F7", "f8": "F8", "f9": "F9", "f10": "F10", "f11": "F11", "f12": "F12",
    "home": "Home",
    "insert": "Insert",
    "left": "Left",
    "menu": "Menu",
    "meta": "Meta_L",
    "num_lock": "Num_Lock",
    "page_down": "Page_Down",
    "page_up": "Page_Up",
    "pause": "Pause",
    "print": "Print",
    "right": "Right",
    "scroll_lock": "Scroll_Lock",
    "shift": "Shift_L",
    "shift_left": "Shift_L",
    "shift_right": "Shift_R",
    "space": "space",
    "super": "Super_L",
    "super_left": "Super_L",
    "super_right": "Super_R",
    "tab": "Tab",
    "up": "Up",
    "win": "Super_L",
    "windows": "Super_L",
}

MOUSE_BUTTONS = {"left": 1, "right": 3, "middle": 2}


def map_key(key: str) -> str:
    return KEYS.get(key.lower(), key)


def _desktop_env(proxy_info: dict | None) -> dict:
    env = dict(_proxy_env(proxy_info))
    env["DISPLAY"] = DISPLAY
    return env


def _run(sandbox, proxy_info, cmd: str, **kwargs):
    return sandbox.commands.run(cmd, envs=_desktop_env(proxy_info), **kwargs)


def _display_up(sandbox, proxy_info) -> bool:
    try:
        result = _run(sandbox, proxy_info, f"xdpyinfo -display {DISPLAY}")
        return result.exit_code == 0
    except Exception:
        return False


def ensure_desktop(sandbox, proxy_info, resolution: tuple[int, int] = (1024, 768)) -> None:
    """Idempotent: start Xvfb + xfce4 only if the display isn't already up.

    Safe to call before every action — a resumed sandbox may have lost its X server
    (E2B pause/resume, sandbox recycling), and this repairs it transparently instead of
    making every tool call check first.
    """
    if _display_up(sandbox, proxy_info):
        return
    width, height = resolution
    _run(
        sandbox, proxy_info,
        f"Xvfb {DISPLAY} -ac -screen 0 {width}x{height}x24 -retro -dpi 96 "
        f"-nolisten tcp -nolisten unix",
        background=True, timeout=0,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if _display_up(sandbox, proxy_info):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("Could not start Xvfb (display never came up)")
    _run(sandbox, proxy_info, "startxfce4", background=True, timeout=0)
    # xfce4-session takes a moment to paint a usable desktop.
    time.sleep(2)


def screenshot(sandbox, proxy_info) -> bytes:
    ensure_desktop(sandbox, proxy_info)
    path = f"/tmp/screenshot-{uuid4()}.png"
    _run(sandbox, proxy_info, f"scrot --pointer {path}")
    data = bytes(sandbox.files.read(path, format="bytes"))
    try:
        sandbox.files.remove(path)
    except Exception:
        pass
    return data


def move_mouse(sandbox, proxy_info, x: int, y: int) -> None:
    ensure_desktop(sandbox, proxy_info)
    _run(sandbox, proxy_info, f"xdotool mousemove --sync {x} {y}")


def click(sandbox, proxy_info, button: str = "left", x: int | None = None, y: int | None = None) -> None:
    ensure_desktop(sandbox, proxy_info)
    if x is not None and y is not None:
        move_mouse(sandbox, proxy_info, x, y)
    _run(sandbox, proxy_info, f"xdotool click {MOUSE_BUTTONS.get(button, 1)}")


def double_click(sandbox, proxy_info, x: int | None = None, y: int | None = None) -> None:
    ensure_desktop(sandbox, proxy_info)
    if x is not None and y is not None:
        move_mouse(sandbox, proxy_info, x, y)
    _run(sandbox, proxy_info, "xdotool click --repeat 2 1")


def scroll(sandbox, proxy_info, direction: str = "down", amount: int = 1) -> None:
    ensure_desktop(sandbox, proxy_info)
    button = "4" if direction == "up" else "5"
    _run(sandbox, proxy_info, f"xdotool click --repeat {amount} {button}")


def drag(sandbox, proxy_info, fr: tuple[int, int], to: tuple[int, int]) -> None:
    ensure_desktop(sandbox, proxy_info)
    move_mouse(sandbox, proxy_info, *fr)
    _run(sandbox, proxy_info, "xdotool mousedown 1")
    move_mouse(sandbox, proxy_info, *to)
    _run(sandbox, proxy_info, "xdotool mouseup 1")


def type_text(sandbox, proxy_info, text: str, chunk_size: int = 25, delay_ms: int = 75) -> None:
    ensure_desktop(sandbox, proxy_info)
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        _run(sandbox, proxy_info, f"xdotool type --delay {delay_ms} -- {shlex.quote(chunk)}")


def press_key(sandbox, proxy_info, keys: str | list[str]) -> None:
    ensure_desktop(sandbox, proxy_info)
    if isinstance(keys, list):
        combo = "+".join(map_key(k) for k in keys)
    else:
        combo = map_key(keys)
    _run(sandbox, proxy_info, f"xdotool key {combo}")


def wait_ms(sandbox, proxy_info, ms: int) -> None:
    _run(sandbox, proxy_info, f"sleep {ms / 1000}")


def open_uri(sandbox, proxy_info, uri: str) -> None:
    ensure_desktop(sandbox, proxy_info)
    handle = _run(sandbox, proxy_info, f"xdg-open {shlex.quote(uri)}", background=True)
    handle.disconnect()


def launch_app(sandbox, proxy_info, application: str) -> None:
    ensure_desktop(sandbox, proxy_info)
    handle = _run(sandbox, proxy_info, f"gtk-launch {shlex.quote(application)}", background=True, timeout=0)
    handle.disconnect()


def get_current_window_id(sandbox, proxy_info) -> str:
    ensure_desktop(sandbox, proxy_info)
    return _run(sandbox, proxy_info, "xdotool getwindowfocus").stdout.strip()


def get_application_windows(sandbox, proxy_info, application: str) -> list[str]:
    ensure_desktop(sandbox, proxy_info)
    out = _run(sandbox, proxy_info, f"xdotool search --onlyvisible --class {shlex.quote(application)}").stdout.strip()
    return out.split("\n") if out else []


def get_window_title(sandbox, proxy_info, window_id: str) -> str:
    ensure_desktop(sandbox, proxy_info)
    return _run(sandbox, proxy_info, f"xdotool getwindowname {shlex.quote(window_id)}").stdout.strip()


def screen_size(sandbox, proxy_info) -> tuple[int, int]:
    ensure_desktop(sandbox, proxy_info)
    result = _run(sandbox, proxy_info, "xrandr")
    match = re.search(r"current (\d+) x (\d+)", result.stdout)
    if not match:
        raise RuntimeError(f"Could not parse screen size from xrandr output: {result.stdout}")
    return int(match.group(1)), int(match.group(2))


def cursor_position(sandbox, proxy_info) -> tuple[int, int]:
    ensure_desktop(sandbox, proxy_info)
    result = _run(sandbox, proxy_info, "xdotool getmouselocation")
    match = re.search(r"x:(\d+)\s+y:(\d+)", result.stdout)
    if not match:
        raise RuntimeError(f"Could not parse cursor position from: {result.stdout}")
    return int(match.group(1)), int(match.group(2))


def _vnc_running(sandbox, proxy_info) -> bool:
    try:
        return _run(sandbox, proxy_info, "pgrep -x x11vnc").exit_code == 0
    except Exception:
        return False


def _wait_for_port(sandbox, proxy_info, port: int, timeout: int = 10) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _run(sandbox, proxy_info, f"netstat -tuln | grep ':{port} '")
        if result.stdout.strip():
            return True
        time.sleep(0.5)
    return False


_PASSWORD_MARKER = "/home/user/.novnc_web_password"


def start_stream(sandbox, proxy_info) -> str:
    """Start x11vnc + noVNC (view-only capable), returning a ready-to-share URL.

    Idempotent: if a stream is already running, reuses the password from the first
    start (stashed in a marker file) instead of generating a new one that wouldn't
    match the running x11vnc's stored password — a run may call this more than once
    across a long computer-use session.
    """
    ensure_desktop(sandbox, proxy_info)

    if _vnc_running(sandbox, proxy_info):
        password = _run(sandbox, proxy_info, f"cat {_PASSWORD_MARKER}").stdout.strip()
    else:
        password = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
        _run(sandbox, proxy_info, "mkdir -p ~/.vnc")
        _run(sandbox, proxy_info, f"x11vnc -storepasswd {password} ~/.vnc/passwd")
        _run(sandbox, proxy_info, f"echo {shlex.quote(password)} > {_PASSWORD_MARKER}")
        _run(
            sandbox, proxy_info,
            f"x11vnc -bg -display {DISPLAY} -forever -wait 50 -shared "
            f"-rfbport {_VNC_PORT} -usepw 2>/tmp/x11vnc_stderr.log",
        )
        _run(
            sandbox, proxy_info,
            f"cd /opt/noVNC/utils && ./novnc_proxy --vnc localhost:{_VNC_PORT} "
            f"--listen {_NOVNC_PORT} --web /opt/noVNC > /tmp/novnc.log 2>&1",
            background=True, timeout=0,
        )
        if not _wait_for_port(sandbox, proxy_info, _NOVNC_PORT):
            raise RuntimeError("Could not start noVNC server")

    host = sandbox.get_host(_NOVNC_PORT)
    return f"https://{host}/vnc.html?autoconnect=true&view_only=true&resize=scale&password={password}"
