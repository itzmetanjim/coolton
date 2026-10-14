import os

import pytest
from cryptography.fernet import Fernet

from agent import mcp_server_store as store


@pytest.fixture
def tmp_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "MCP_SERVER_STORE_FILE", str(tmp_path / "mcp_server_store.json"))
    monkeypatch.setattr(store, "MCP_SERVER_KEY_FILE", str(tmp_path / "mcp_server_key.bin"))
    monkeypatch.setenv("MCP_SERVER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return tmp_path


def test_add_server_and_get_decrypted(tmp_store):
    server_id = store.add_server("U1", "My Notion", "https://mcp.example.com/mcp/", "secret-token")
    assert server_id.startswith("mcp_")
    server = store.get_server_decrypted("U1", server_id)
    assert server["name"] == "My Notion"
    assert server["url"] == "https://mcp.example.com/mcp"  # trailing slash stripped
    assert server["token"] == "secret-token"


def test_add_server_without_token(tmp_store):
    server_id = store.add_server("U1", "No auth", "https://mcp.example.com/mcp")
    server = store.get_server_decrypted("U1", server_id)
    assert server["token"] is None


def test_get_user_servers_hides_token(tmp_store):
    store.add_server("U1", "n", "https://mcp.example.com/mcp", "supersecret")
    servers = store.get_user_servers("U1")
    assert len(servers) == 1
    assert "token" not in servers[0]
    assert servers[0]["name"] == "n"


def test_get_server_decrypted_unknown_returns_none(tmp_store):
    assert store.get_server_decrypted("U1", "mcp_unknown") is None


def test_add_server_requires_name(tmp_store):
    with pytest.raises(ValueError):
        store.add_server("U1", "  ", "https://mcp.example.com/mcp")


def test_add_server_enforces_max_per_user(tmp_store):
    for i in range(store.MAX_SERVERS_PER_USER):
        store.add_server("U1", f"server{i}", f"https://mcp{i}.example.com/mcp")
    with pytest.raises(ValueError):
        store.add_server("U1", "one too many", "https://over.example.com/mcp")


def test_delete_server(tmp_store):
    server_id = store.add_server("U1", "n", "https://mcp.example.com/mcp")
    store.delete_server("U1", server_id)
    assert store.get_server_decrypted("U1", server_id) is None
    assert store.get_user_servers("U1") == []


def test_delete_unknown_server_is_noop(tmp_store):
    store.delete_server("U1", "mcp_missing")  # must not raise


def test_servers_are_scoped_per_user(tmp_store):
    server_id = store.add_server("U1", "n", "https://mcp.example.com/mcp")
    assert store.get_user_servers("U2") == []
    assert store.get_server_decrypted("U2", server_id) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://mcp.example.com/mcp",
        "https://127.0.0.1/mcp",
        "https://localhost/mcp",
        "https://169.254.169.254/mcp",
        "https://user:pass@mcp.example.com/mcp",
    ],
)
def test_add_server_rejects_unsafe_urls(tmp_store, url):
    with pytest.raises(ValueError):
        store.add_server("U1", "unsafe", url)


def test_key_generated_when_no_env(tmp_store, monkeypatch, tmp_path):
    monkeypatch.delenv("MCP_SERVER_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(store, "MCP_SERVER_KEY_FILE", str(tmp_path / "fresh_key.bin"))
    assert not os.path.exists(store.MCP_SERVER_KEY_FILE)
    server_id = store.add_server("U1", "n", "https://mcp.example.com/mcp", "tok")
    assert store.get_server_decrypted("U1", server_id)["token"] == "tok"
    assert os.path.exists(store.MCP_SERVER_KEY_FILE)


def test_probe_server_reports_failure_for_unreachable_host(tmp_store, monkeypatch):
    async def refused(url, transport, token, headers):
        return False, "connection refused"

    monkeypatch.setattr(store, "_try", refused)
    ok, detail, _ = store.probe_server("https://mcp.unreachable.example/mcp", "")
    assert ok is False
    assert "Streamable HTTP" in detail and "SSE" in detail  # auto-detect tried both


def test_probe_server_refuses_a_host_that_doesnt_resolve(tmp_store):
    ok, detail, _ = store.probe_server("https://this-host-should-not-resolve.invalid/mcp", "")
    assert ok is False and "doesn't resolve" in detail


def test_auto_detect_keeps_whichever_transport_the_server_speaks(tmp_store, monkeypatch):
    tried = []

    async def fake_try(url, transport, token, headers):
        tried.append(transport)
        return transport == "sse", "ok" if transport == "sse" else "405 Method Not Allowed"

    monkeypatch.setattr(store, "_try", fake_try)

    assert store.probe_server("https://mcp.example.com/mcp")[0::2] == (True, "sse")
    assert tried == ["http", "sse"]
    tried.clear()
    store.probe_server("https://mcp.example.com/sse")
    assert tried == ["sse"]  # a /sse URL tries SSE first


def test_servers_keep_their_transport_and_encrypted_headers(tmp_store):
    """API-key headers are secrets like the token; servers saved before transports
    existed were Streamable HTTP."""
    headers = store.parse_headers("X-API-Key: abc123\n\nX-Org: acme")
    server_id = store.add_server("U1", "Keyed", "https://mcp.example.com/sse", "", "sse", headers)

    server = store.get_server_decrypted("U1", server_id)
    assert server["transport"] == "sse" and server["headers"] == {"X-API-Key": "abc123", "X-Org": "acme"}
    assert "abc123" not in open(store.MCP_SERVER_STORE_FILE).read()
    assert store.request_headers("tok", {"Authorization": "Basic x"}) == {"Authorization": "Basic x"}
    with pytest.raises(ValueError):
        store.parse_headers("not a header")


def test_probe_never_connects_to_a_local_or_private_address(tmp_store, monkeypatch):
    """The add-server form probes before saving: the URL is checked first, so it can't
    be used to make coolton's server request its own local services."""
    tried = []
    monkeypatch.setattr(store, "_try", lambda *a: tried.append(a))
    for url in ("http://127.0.0.1:29057/agent_tools", "https://10.0.0.1/mcp", "https://localhost/mcp"):
        ok, detail, _ = store.probe_server(url)
        assert not ok and detail
    assert tried == []


def test_every_request_to_a_users_mcp_server_is_rechecked_including_redirects():
    """The URL is checked when it's saved, but the MCP client follows redirects by itself:
    a public server could otherwise send coolton to a service on its own host."""
    import asyncio

    import httpx

    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": next_hop})

    async def call():
        client = store._public_only_client()
        client._transport = httpx.MockTransport(handler)
        async with client:
            await client.post("https://mcp.example.com/mcp", json={})

    for next_hop in ("http://127.0.0.1:29057/agent_tools/x/y", "https://10.0.0.5/admin", "https://169.254.169.254/"):
        seen.clear()
        with pytest.raises(ValueError):
            asyncio.run(call())
        assert seen == ["https://mcp.example.com/mcp"]
