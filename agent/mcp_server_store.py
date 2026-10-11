"""Per-user MCP server registrations.

coolton ships with one built-in MCP connection (Slack's own hosted server, see
agent/platforms/slack.py). This lets any user extend coolton with their own
MCP servers from App Home — the same capability gorkie exposes via its
`mcp/user-servers.ts` per-user client cache. A server's URL is public
configuration; its bearer token and any extra headers (API keys) are secrets and
encrypted at rest, mirroring agent/byok_store.py. Remote servers work over either MCP
transport: Streamable HTTP or the older HTTP+SSE.
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


TRANSPORTS = ("http", "sse")  # Streamable HTTP, and the older HTTP+SSE transport
_PROBE_TIMEOUT_SECONDS = 20
_MAX_HEADERS = 10
_HEADER_NAME_CHARS = set("!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def parse_headers(text: str) -> dict[str, str]:
    """Extra request headers, one `Name: value` per line (e.g. `X-API-Key: abc`), for
    servers that authenticate some other way than a bearer token."""
    headers: dict[str, str] = {}
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        name, sep, value = line.partition(":")
        name, value = name.strip(), value.strip()
        if not sep or not name or not value or not set(name) <= _HEADER_NAME_CHARS:
            raise ValueError(f"Each header must be one `Name: value` line; can't read {line.strip()!r}")
        headers[name] = value
    if len(headers) > _MAX_HEADERS:
        raise ValueError(f"At most {_MAX_HEADERS} extra headers")
    return headers


def request_headers(token: str | None, headers: dict | None) -> dict[str, str]:
    """The headers to send: the extra ones, plus `Authorization: Bearer <token>` unless an
    extra header already sets Authorization."""
    merged = dict(headers or {})
    if token and not any(k.lower() == "authorization" for k in merged):
        merged["Authorization"] = f"Bearer {token}"
    return merged


def build_transport(url: str, transport: str, token: str | None = None, headers: dict | None = None):
    from pydantic_ai.mcp import SSETransport, StreamableHttpTransport

    cls = SSETransport if transport == "sse" else StreamableHttpTransport
    return cls(url, headers=request_headers(token, headers))


def get_user_servers(user_id: str) -> list[dict]:
    """Non-secret view for App Home: id, name, url, transport. Never the token or headers."""
    with store_lock:
        data = _load()
        servers = data.get(user_id, {})
        return [{"id": sid, "name": s["name"], "url": s["url"], "transport": s.get("transport", "http")}
                for sid, s in servers.items()]


def get_server_decrypted(user_id: str, server_id: str) -> dict | None:
    with store_lock:
        data = _load()
        s = data.get(user_id, {}).get(server_id)
    if not s:
        return None
    token, headers = None, {}
    try:
        if s.get("token_encrypted"):
            token = _dec(_get_fernet(), s["token_encrypted"])
        if s.get("headers_encrypted"):
            headers = json.loads(_dec(_get_fernet(), s["headers_encrypted"]))
    except Exception:
        logger.exception("Failed to decrypt MCP server secrets for %s/%s", user_id, server_id)
        return None
    # Servers saved before the transport choice existed were all Streamable HTTP.
    return {"id": server_id, "name": s["name"], "url": s["url"], "token": token,
            "transport": s.get("transport", "http"), "headers": headers}


def add_server(user_id: str, name: str, url: str, token: str = "", transport: str = "http",
               headers: dict | None = None) -> str:
    if not name or not name.strip():
        raise ValueError("Server name is required")
    if transport not in TRANSPORTS:
        raise ValueError(f"transport must be one of {', '.join(TRANSPORTS)}")
    url = validate_endpoint_url(url)
    server_id = _new_id()
    with store_lock:
        data = _load()
        user_servers = data.setdefault(user_id, {})
        if len(user_servers) >= MAX_SERVERS_PER_USER:
            raise ValueError(f"You already have {MAX_SERVERS_PER_USER} MCP servers registered (the max) — delete one first")
        entry = {"name": name.strip(), "url": url, "transport": transport}
        if token:
            entry["token_encrypted"] = _enc(_get_fernet(), token)
        if headers:  # may hold API keys, so encrypted like the token
            entry["headers_encrypted"] = _enc(_get_fernet(), json.dumps(headers))
        user_servers[server_id] = entry
        _save(data)
    return server_id


def delete_server(user_id: str, server_id: str) -> None:
    with store_lock:
        data = _load()
        data.get(user_id, {}).pop(server_id, None)
        _save(data)


async def _try(url: str, transport: str, token: str | None, headers: dict | None) -> tuple[bool, str]:
    from pydantic_ai.mcp import MCPToolset

    toolset = MCPToolset(build_transport(url, transport, token, headers))

    async def connect() -> int:
        async with toolset:
            return len(await toolset.list_tools())

    try:
        count = await asyncio.wait_for(connect(), _PROBE_TIMEOUT_SECONDS)
        return True, f"connected over {'SSE' if transport == 'sse' else 'Streamable HTTP'}, {count} tool(s) available"
    except asyncio.TimeoutError:
        return False, f"no answer within {_PROBE_TIMEOUT_SECONDS}s"
    except Exception as e:
        return False, str(e) or type(e).__name__


async def _probe(url: str, token: str | None, transport: str, headers: dict | None) -> tuple[bool, str, str]:
    if transport in TRANSPORTS:
        order = [transport]
    else:  # auto: whichever the server speaks, trying the likelier one first
        order = ["sse", "http"] if url.rstrip("/").endswith("/sse") else ["http", "sse"]
    failures = []
    for candidate in order:
        ok, detail = await _try(url, candidate, token, headers)
        if ok:
            return True, detail, candidate
        failures.append(f"{'SSE' if candidate == 'sse' else 'Streamable HTTP'}: {detail}")
    return False, "; ".join(failures), order[0]


def probe_server(url: str, token: str = "", transport: str = "auto", headers: dict | None = None) -> tuple[bool, str, str]:
    """Connect to an MCP server and list its tools, so a bad URL, transport or credential
    is caught when the user adds it rather than silently on every future turn
    (agent/platforms/slack.py's toolsets() just drops a toolset that fails to build).
    `transport` "auto" tries Streamable HTTP and SSE. Returns (ok, detail, transport)."""
    return asyncio.run(_probe(url, token or None, transport, headers))
