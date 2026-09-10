"""Tests for tool_proxy.py's HTTP auth boundary: the only thing standing between
sandboxed code and calling arbitrary/non-allowlisted tools with another thread's
credentials. Spins up the real (singleton) server and hits it over HTTP, since the
auth logic lives inside _Handler.do_POST, not in a unit-testable helper."""

import json
import time
import uuid

import requests

import agent.tool_proxy as tool_proxy
from agent import redact

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


# ---------------------------------------------------------------------------
# Redaction — this handler is the ONE place a raw secret returned by a nested
# tool call (agent_tools.<name>(...) from inside code_mode) ever gets handed
# back into the sandbox. Every other tool-call path is redacted by
# agent.plan_block's hooks, but those never see this call at all — code_mode's
# own outer result *is* redacted there, by a plain substring scan, which is
# exactly what can't catch a secret the sandboxed code already transformed
# (base64, split across variables, ...) before printing it. Redacting has to
# happen here, before the sandbox ever receives the plaintext.
# ---------------------------------------------------------------------------


def test_tool_result_containing_a_secret_is_redacted_before_reaching_the_sandbox(monkeypatch):
    monkeypatch.setenv("FAKE_TOOL_PROXY_API_KEY", "sk-supersecretvalue")
    redact.invalidate_secret_cache()
    try:
        token, sandbox_id, _, _ = _register(result="here is the key: sk-supersecretvalue")
        resp = requests.post(
            f"{BASE}/{sandbox_id}/echo_tool",
            json={"args": [], "kwargs": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        body = resp.json()
        assert body["ok"] is True
        assert "sk-supersecretvalue" not in body["result"]
        assert "***" in body["result"]
    finally:
        redact.invalidate_secret_cache()


def test_tool_error_containing_a_secret_is_redacted(monkeypatch):
    secret = "sk-errorpathsecretvalue"
    monkeypatch.setenv("FAKE_TOOL_PROXY_ERROR_KEY", secret)
    redact.invalidate_secret_cache()
    try:
        token = uuid.uuid4().hex
        sandbox_id = uuid.uuid4().hex

        def failing_tool(ctx, *a, **k):
            raise ValueError(f"failed while using {secret}")

        tool_proxy.register_sandbox(sandbox_id, token, object(), lambda n: failing_tool, ["failing_tool"])
        resp = requests.post(
            f"{BASE}/{sandbox_id}/failing_tool",
            json={"args": [], "kwargs": {}},
            headers={"Authorization": f"Bearer {token}"},
        )
        body = resp.json()
        assert body["ok"] is False
        assert secret not in body["error"]
        assert "***" in body["error"]
    finally:
        redact.invalidate_secret_cache()


def test_sandbox_code_cannot_launder_a_secret_through_base64(monkeypatch):
    """The exact scenario this exists for: code_mode code that fetches a tool
    result and transforms it (base64, here) before printing/returning it.
    Redacting only code_mode's OWN final output (a substring scan) can't
    catch a transformed secret — the raw value must never reach the sandbox
    module in the first place. Exercises the real HTTP round trip (this
    module's own generated agent_tools code -> the real running server), not
    a mocked urlopen.
    """
    import base64
    import os
    import types

    secret = "sk-lauderingtestsecretvalue"
    monkeypatch.setenv("FAKE_TOOL_PROXY_LAUNDER_KEY", secret)
    redact.invalidate_secret_cache()
    try:
        token, sandbox_id, _, _ = _register(result=f"your token is {secret}")

        monkeypatch.setenv("AGENT_TOOLS_BASE", BASE)
        monkeypatch.setenv("AGENT_TOOLS_TOKEN", token)
        monkeypatch.setenv("AGENT_TOOLS_SANDBOX", sandbox_id)

        code = tool_proxy.build_sandbox_module(["echo_tool"], {"echo_tool": "()"})
        module = types.ModuleType("agent_tools")
        module.__dict__["os"] = os
        exec(compile(code, "agent_tools.py", "exec"), module.__dict__)

        raw = module.echo_tool()
        assert secret not in raw
        assert "***" in raw

        laundered = base64.b64encode(raw.encode()).decode()
        assert base64.b64encode(secret.encode()).decode() not in laundered
    finally:
        redact.invalidate_secret_cache()


def test_tool_result_without_a_secret_is_unaffected():
    token, sandbox_id, _, _ = _register(result="nothing sensitive here")
    resp = requests.post(
        f"{BASE}/{sandbox_id}/echo_tool",
        json={"args": [], "kwargs": {}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.json()["result"] == "nothing sensitive here"


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
