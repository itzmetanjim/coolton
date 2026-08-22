"""Per-user MCP server registrations.

coolton ships with one built-in MCP connection (Slack's own hosted server, see
agent/platforms/slack.py). This lets any user extend coolton with their own
MCP servers from App Home — the same capability gorkie exposes via its
`mcp/user-servers.ts` per-user client cache. A server's URL is public
configuration; its bearer token (if any) is a secret and encrypted at rest,
mirroring agent/byok_store.py.
"""

import asyncio
import base64
import json
import logging
import os
import tempfile
import threading
import uuid

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from agent.byok_store import validate_endpoint_url

logger = logging.getLogger(__name__)

MCP_SERVER_STORE_FILE = "mcp_server_store.json"
MCP_SERVER_KEY_FILE = "mcp_server_key.bin"
MCP_SERVER_ENV_KEY = "MCP_SERVER_ENCRYPTION_KEY"
MAX_SERVERS_PER_USER = 5

store_lock = threading.Lock()
_key_lock = threading.Lock()


def _get_fernet() -> Fernet:
    key = os.environ.get(MCP_SERVER_ENV_KEY)
    if key:
        key_bytes = key.encode() if isinstance(key, str) else key
        if len(key_bytes) != 44:
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b"coolton-mcp-server-salt",
                iterations=100000,
            )
            key_bytes = base64.urlsafe_b64encode(kdf.derive(key_bytes))
        else:
            key_bytes = key_bytes if isinstance(key_bytes, bytes) else key_bytes.encode()
        return Fernet(key_bytes)

    with _key_lock:
        if os.path.exists(MCP_SERVER_KEY_FILE):
            with open(MCP_SERVER_KEY_FILE, "rb") as f:
                return Fernet(f.read().strip())

        key = Fernet.generate_key()
        fd, name = tempfile.mkstemp(
            prefix="mcp-server-key-", dir=os.path.dirname(os.path.abspath(MCP_SERVER_KEY_FILE)) or "."
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
            os.replace(name, MCP_SERVER_KEY_FILE)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        logger.info("Generated new MCP server encryption key at %s", MCP_SERVER_KEY_FILE)
        return Fernet(key)


def _load() -> dict:
    if not os.path.exists(MCP_SERVER_STORE_FILE):
        return {}
    with open(MCP_SERVER_STORE_FILE, "r") as f:
        return json.load(f)


def _save(data: dict) -> None:
    temp = f"{MCP_SERVER_STORE_FILE}.tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, MCP_SERVER_STORE_FILE)


def _enc(fernet: Fernet, value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def _dec(fernet: Fernet, encrypted: str) -> str:
    return fernet.decrypt(encrypted.encode()).decode()


def _new_id() -> str:
    return "mcp_" + uuid.uuid4().hex[:10]


def get_user_servers(user_id: str) -> list[dict]:
    """Non-secret view for App Home: id, name, url — never the token."""
    with store_lock:
        data = _load()
        servers = data.get(user_id, {})
        return [{"id": sid, "name": s["name"], "url": s["url"]} for sid, s in servers.items()]


def get_server_decrypted(user_id: str, server_id: str) -> dict | None:
    with store_lock:
        data = _load()
        s = data.get(user_id, {}).get(server_id)
    if not s:
        return None
    token = None
    if s.get("token_encrypted"):
        fernet = _get_fernet()
        try:
            token = _dec(fernet, s["token_encrypted"])
        except Exception:
            logger.exception("Failed to decrypt MCP server token for %s/%s", user_id, server_id)
            return None
    return {"id": server_id, "name": s["name"], "url": s["url"], "token": token}


def add_server(user_id: str, name: str, url: str, token: str = "") -> str:
    if not name or not name.strip():
        raise ValueError("Server name is required")
    url = validate_endpoint_url(url)
    server_id = _new_id()
    with store_lock:
        data = _load()
        user_servers = data.setdefault(user_id, {})
        if len(user_servers) >= MAX_SERVERS_PER_USER:
            raise ValueError(f"You already have {MAX_SERVERS_PER_USER} MCP servers registered (the max) — delete one first")
        entry = {"name": name.strip(), "url": url}
        if token:
            entry["token_encrypted"] = _enc(_get_fernet(), token)
        user_servers[server_id] = entry
        _save(data)
    return server_id


def delete_server(user_id: str, server_id: str) -> None:
    with store_lock:
        data = _load()
        data.get(user_id, {}).pop(server_id, None)
        _save(data)


async def _probe(url: str, token: str) -> tuple[bool, str]:
    from pydantic_ai.mcp import MCPToolset, StreamableHttpTransport

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = StreamableHttpTransport(url, headers=headers)
    toolset = MCPToolset(transport)
    try:
        async with toolset:
            tools = await toolset.list_tools()
        return True, f"connected, {len(tools)} tool(s) available"
    except Exception as e:
        return False, str(e)


def probe_server(url: str, token: str = "") -> tuple[bool, str]:
    """Synchronously connect to an MCP server and list its tools, so a bad
    URL/token is caught when the user adds it rather than silently on every
    future turn (agent/platforms/slack.py's toolsets() just drops a toolset
    that fails to build)."""
    return asyncio.run(_probe(url, token))
