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


def test_probe_server_reports_failure_for_unreachable_host(tmp_store):
    ok, detail = store.probe_server("https://this-host-should-not-resolve.invalid/mcp", "")
    assert ok is False
    assert detail
