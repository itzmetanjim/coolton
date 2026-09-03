"""Host-side HTTP API that lets sandboxed code call coolton's own tools programmatically.

coolton's `code_mode` tool writes a Python program into an E2B sandbox. That program calls
`agent_tools.<tool_name>(...)`; the sandboxed helper module POSTs to

    https://ghproxy.tanjim.org/agent_tools/<sandbox_id>/<tool_name>

(TLS terminated by Caddy -> github_proxy routes /agent_tools/* to this localhost service),
which looks up the tool registered for that sandbox/thread and executes it with the thread's
AgentDeps, then returns the string result (or parsed JSON for the generic Slack API tools).

Security:
  * Requests must present the per-sandbox token (the same token github_proxy allowlists for
    git traffic). The tool proxy only knows about tokens/sandboxes registered by `code_mode`
    for this process, so a token can never be used outside its own thread's deps.
  * Only allowlisted tools can be called. Sandbox tools (recursion), `code_mode` itself, and
    control-flow tools (`skip`, `leave_thread_tool`, `join_thread_tool`) are excluded.
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pydantic_ai import RunContext
from pydantic_ai.usage import RunUsage

logger = logging.getLogger(__name__)

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 29057

# Prefix github_proxy routes to us (mirror of the URL path, minus the leading host).
URL_PREFIX = "/agent_tools"

_reg_lock = threading.Lock()
_registrations: dict[str, dict] = {}  # token -> {sandbox_id, deps, resolver, allowlist}
_server = None
_server_lock = threading.Lock()


def register_sandbox(sandbox_id: str, token: str, deps, resolver, allowlist) -> None:
    """Bind a per-sandbox token to this thread's AgentDeps + the tool resolver/allowlist.

    `resolver` is a callable mapping a tool name to the raw tool function (which takes
    RunContext as its first arg). Subsequent POSTs with this token call tools with a
    fresh `RunContext(deps=...)` built from these deps.
    """
    with _reg_lock:
        _registrations[token] = {
            "sandbox_id": sandbox_id,
            "deps": deps,
            "resolver": resolver,
            "allowlist": set(allowlist),
        }


def unregister_sandbox(sandbox_id: str) -> None:
    with _reg_lock:
        for tok in [t for t, r in _registrations.items() if r["sandbox_id"] == sandbox_id]:
            del _registrations[tok]


def _ensure_server() -> ThreadingHTTPServer:
    global _server
    with _server_lock:
        if _server is None:
            _server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), _Handler)
            t = threading.Thread(target=_server.serve_forever, daemon=True, name="tool-proxy")
            t.start()
            logger.info("tool_proxy listening on %s:%s", LISTEN_HOST, LISTEN_PORT)
    return _server


def start() -> None:
    _ensure_server()


class _Handler(BaseHTTPRequestHandler):
    server_version = "coolton-tool-proxy/1.0"

    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        parts = path.split("/")
        # /agent_tools/<sandbox_id>/<tool_name>
        if len(parts) != 4 or parts[1] != "agent_tools" or not parts[2] or not parts[3]:
            return self._send_json(404, {"ok": False, "error": "not found"})
        sandbox_id, tool_name = parts[2], parts[3]

        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        with _reg_lock:
            reg = _registrations.get(token) if token else None
        if not reg or reg["sandbox_id"] != sandbox_id:
            return self._send_json(403, {"ok": False, "error": "unauthorized sandbox or token"})
        if tool_name not in reg["allowlist"]:
            return self._send_json(403, {"ok": False, "error": f"tool not allowed: {tool_name}"})

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or b"{}")
        except Exception as e:
            return self._send_json(400, {"ok": False, "error": f"bad body: {e}"})

        func = reg["resolver"](tool_name)
        if func is None:
            return self._send_json(404, {"ok": False, "error": f"unknown tool: {tool_name}"})

        args = body.get("args", []) or []
        kwargs = body.get("kwargs", {}) or {}
        if not isinstance(args, list) or not isinstance(kwargs, dict):
            return self._send_json(400, {"ok": False, "error": "args must be a list, kwargs a dict"})
        try:
            result = func(RunContext(deps=reg["deps"], model=None, usage=RunUsage()), *args, **kwargs)
            if result is None:
                result = ""
            if not isinstance(result, str):
                result = str(result)
            return self._send_json(200, {"ok": True, "result": result})
        except Exception as e:
            logger.warning("tool_proxy call failed for %s: %s", tool_name, e)
            return self._send_json(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def log_message(self, *a):  # silence request logging
        pass


# ---------------------------------------------------------------------------
# Sandbox-side helper module (written into /home/user/agent_tools.py in the E2B
# sandbox). __ALLOWED__ and __SIGNATURES__ are injected before writing.
# ---------------------------------------------------------------------------
SANDBOX_MODULE_TEMPLATE = '''"""agent_tools -- call coolton's own tools programmatically.

Every allowlisted tool is exposed as `agent_tools.<name>(*args, **kwargs)`.
Most tools return a human-readable string. The generic Slack API tools
(`slack_api_call`, `slack_api_call_as_bot_tool`) return the parsed JSON payload
(dict) so you can iterate over results directly.

Check `agent_tools.help()` for available tools and signatures.
"""
import ast
import json
import os
import urllib.error
import urllib.request

_BASE = os.environ["AGENT_TOOLS_BASE"]
_TOKEN = os.environ["AGENT_TOOLS_TOKEN"]
_SANDBOX = os.environ["AGENT_TOOLS_SANDBOX"]
_STRUCTURED = {"slack_api_call", "slack_api_call_as_bot_tool"}

__ALLOWED__ = __ALLOWED__
__SIGNATURES__ = __SIGNATURES__


def _encode_compound(v):
    # Every real tool parameter is now a plain scalar (str/int/bool/float) — the
    # tools that used to take a schema-less dict param (e.g. slack_api_call's
    # api_parameters) take a JSON-encoded string instead, since models constructing
    # a JSON-schema tool call struggled with an untyped nested object. That
    # weakness doesn't apply here: this is ordinary Python code the model wrote
    # itself, where a dict/list literal is completely natural — encode it to the
    # JSON string the tool actually expects, transparently, instead of making
    # every call site spell out json.dumps(...) by hand.
    if isinstance(v, (dict, list)):
        return json.dumps(v)
    return v


def _call(name, args, kwargs):
    args = [_encode_compound(a) for a in args]
    kwargs = {k: _encode_compound(v) for k, v in kwargs.items()}
    body = json.dumps({"name": name, "args": args, "kwargs": kwargs}).encode("utf-8")
    req = urllib.request.Request(
        _BASE + "/" + _SANDBOX + "/" + name,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + _TOKEN},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": "HTTP %s from tool proxy" % e.code}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}


def _make(name, sig):
    def fn(*args, **kwargs):
        res = _call(name, list(args), kwargs)
        if not res.get("ok"):
            raise RuntimeError("%s failed: %s" % (name, res.get("error", "unknown error")))
        result = res.get("result")
        if name in _STRUCTURED and isinstance(result, str) and result.startswith("Success: "):
            try:
                result = ast.literal_eval(result[len("Success: "):])
            except Exception:
                pass
        return result
    fn.__name__ = name
    fn.__doc__ = "%s%s" % (name, sig or "")
    return fn


for _n in __ALLOWED__:
    globals()[_n] = _make(_n, __SIGNATURES__.get(_n, ""))


def help():  # noqa: A001
    lines = ["Available agent_tools:"]
    for _n in __ALLOWED__:
        lines.append("  %s%s" % (_n, __SIGNATURES__.get(_n, "")))
    return "\\n".join(lines)


__all__ = list(__ALLOWED__)
'''


def build_sandbox_module(allowlist: list[str], signatures: dict[str, str]) -> str:
    code = SANDBOX_MODULE_TEMPLATE
    code = code.replace("__ALLOWED__ = __ALLOWED__", "__ALLOWED__ = " + json.dumps(allowlist))
    code = code.replace("__SIGNATURES__ = __SIGNATURES__", "__SIGNATURES__ = " + json.dumps(signatures))
    return code


def format_signatures(registry) -> dict[str, str]:
    """Build {tool_name: "name(arg: type, ...)"} from the agent tool registry."""
    out = {}
    for name, td in registry.items():
        try:
            schema = td.tool_def.parameters_json_schema
        except Exception:
            continue
        props = schema.get("properties", {}) or {}
        required = set(schema.get("required", []) or [])
        parts = []
        for k, p in props.items():
            ptype = p.get("type", "any")
            if k in required:
                parts.append(f"{k}: {ptype}")
            else:
                parts.append(f"{k}: {ptype}=...")
        out[name] = f"({', '.join(parts)})"
    return out
