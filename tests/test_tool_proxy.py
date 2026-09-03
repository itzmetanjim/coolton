"""Tests for tool_proxy.py's HTTP auth boundary: the only thing standing between
sandboxed code and calling arbitrary/non-allowlisted tools with another thread's
credentials. Spins up the real (singleton) server and hits it over HTTP, since the
auth logic lives inside _Handler.do_POST, not in a unit-testable helper."""

import json
import time
import uuid

import requests

import agent.tool_proxy as tool_proxy

BASE = f"http://{tool_proxy.LISTEN_HOST}:{tool_proxy.LISTEN_PORT}{tool_proxy.URL_PREFIX}"


def setup_module(module):
    tool_proxy.start()
    # The server binds synchronously in _ensure_server, but give the accept loop
    # a moment on slow CI runners before the first request.
    for _ in range(50):
        try:
            requests.post(f"{BASE}/nonexistent/nonexistent", timeout=1)
            break
        except requests.exceptions.ConnectionError:
            time.sleep(0.05)


def _register(tool_name="echo_tool", result="ok"):
    token = uuid.uuid4().hex
    sandbox_id = uuid.uuid4().hex
    seen_ctx = {}

    def echo_tool(ctx, *args, **kwargs):
        seen_ctx["ctx"] = ctx
        seen_ctx["args"] = args
        seen_ctx["kwargs"] = kwargs
        return result

    def resolver(name):
        return echo_tool if name == tool_name else None

    deps = object()
    tool_proxy.register_sandbox(sandbox_id, token, deps, resolver, [tool_name])
    return token, sandbox_id, deps, seen_ctx


def test_wrong_token_rejected():
    token, sandbox_id, _, _ = _register()
    resp = requests.post(
        f"{BASE}/{sandbox_id}/echo_tool",
        json={"args": [], "kwargs": {}},
        headers={"Authorization": "Bearer not-the-real-token"},
    )
    assert resp.status_code == 403
    assert "unauthorized" in resp.json()["error"]


def test_missing_token_rejected():
    _, sandbox_id, _, _ = _register()
    resp = requests.post(f"{BASE}/{sandbox_id}/echo_tool", json={"args": [], "kwargs": {}})
    assert resp.status_code == 403


def test_correct_token_wrong_sandbox_id_rejected():
    """A token issued for sandbox A must not work against sandbox B's URL —
    otherwise one sandbox could call tools bound to a different thread's deps."""
    token, _, _, _ = _register()
    other_sandbox_id = uuid.uuid4().hex
    resp = requests.post(
        f"{BASE}/{other_sandbox_id}/echo_tool",
        json={"args": [], "kwargs": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_tool_not_in_allowlist_rejected():
    token, sandbox_id, _, _ = _register(tool_name="echo_tool")
    resp = requests.post(
        f"{BASE}/{sandbox_id}/some_other_tool",
        json={"args": [], "kwargs": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403
    assert "not allowed" in resp.json()["error"]


def test_valid_call_invokes_resolved_tool_with_bound_deps():
    token, sandbox_id, deps, seen_ctx = _register(result="hello from tool")
    resp = requests.post(
        f"{BASE}/{sandbox_id}/echo_tool",
        json={"args": ["a1"], "kwargs": {"k": "v"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["result"] == "hello from tool"
    assert seen_ctx["ctx"].deps is deps
    assert seen_ctx["args"] == ("a1",)
    assert seen_ctx["kwargs"] == {"k": "v"}


def test_unregistered_sandbox_token_no_longer_works():
    token, sandbox_id, _, _ = _register()
    tool_proxy.unregister_sandbox(sandbox_id)
    resp = requests.post(
        f"{BASE}/{sandbox_id}/echo_tool",
        json={"args": [], "kwargs": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# SANDBOX_MODULE_TEMPLATE / build_sandbox_module — the code written into the
# sandbox itself. Every real tool parameter is now a plain string (see
# agent.agent._parse_json_object_param), but code_mode's own docstring shows
# calling agent_tools.slack_api_call_as_bot_tool(method, {"channel": "..."})
# with a literal dict — that must still work, by JSON-encoding compound args
# transparently before they go over the wire.
# ---------------------------------------------------------------------------


def _exec_sandbox_module(monkeypatch, allowlist, signatures):
    import os
    import types

    monkeypatch.setenv("AGENT_TOOLS_BASE", "http://fake-base/agent_tools")
    monkeypatch.setenv("AGENT_TOOLS_TOKEN", "fake-token")
    monkeypatch.setenv("AGENT_TOOLS_SANDBOX", "fake-sandbox")

    code = tool_proxy.build_sandbox_module(allowlist, signatures)
    module = types.ModuleType("agent_tools")
    module.__dict__["os"] = os
    exec(compile(code, "agent_tools.py", "exec"), module.__dict__)
    return module


def test_sandbox_module_json_encodes_a_dict_argument_before_sending(monkeypatch):
    module = _exec_sandbox_module(
        monkeypatch, ["slack_api_call_as_bot_tool"], {"slack_api_call_as_bot_tool": "(method, api_parameters)"}
    )

    captured = {}

    class _FakeResp:
        def read(self):
            return json.dumps({"ok": True, "result": "Success: {}"}).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)

    # Exactly the pattern shown in code_mode's own docstring: a literal dict.
    module.slack_api_call_as_bot_tool("conversations.members", {"channel": "C0B7QEK0MQB"})

    sent_args = captured["body"]["args"]
    assert sent_args[0] == "conversations.members"
    assert sent_args[1] == '{"channel": "C0B7QEK0MQB"}'
    assert isinstance(sent_args[1], str)


def test_tool_exception_returns_200_with_ok_false():
    token = uuid.uuid4().hex
    sandbox_id = uuid.uuid4().hex

    def failing_tool(ctx, *a, **k):
        raise ValueError("boom")

    tool_proxy.register_sandbox(sandbox_id, token, object(), lambda n: failing_tool, ["failing_tool"])
    resp = requests.post(
        f"{BASE}/{sandbox_id}/failing_tool",
        json={"args": [], "kwargs": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "boom" in body["error"]
