import logging
import threading
import uuid
import ipaddress
from urllib.parse import urlparse
from cryptography.fernet import Fernet
from agent.json_file import load_json, save_json

logger = logging.getLogger(__name__)

BYOK_STORE_FILE = "byok_store.json"
BYOK_KEY_FILE = "byok_key.bin"
BYOK_ENV_KEY = "BYOK_ENCRYPTION_KEY"
store_lock = threading.Lock()


def validate_endpoint_url(base_url: str) -> str:
    """Validate and normalize a user-supplied HTTPS API endpoint.

    BYOK credentials are sent to this URL, so reject non-HTTPS URLs, embedded
    credentials, malformed hosts, local/private network targets, and traversal
    segments before persisting the endpoint.
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("Endpoint URL is required")
    value = base_url.strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        raise ValueError("Endpoint URL must be a valid HTTPS URL")
    if parsed.username or parsed.password:
        raise ValueError("Endpoint URL must not contain embedded credentials")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Endpoint URL contains invalid control characters")
    if any(part in (".", "..") for part in parsed.path.split("/")):
        raise ValueError("Endpoint URL must not contain path traversal segments")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Endpoint URL must include a hostname")
    hostname = hostname.rstrip(".").lower()
    if hostname in {"localhost", "metadata.google.internal", "metadata.google.com"}:
        raise ValueError("Endpoint URL must not target a local or metadata host")
    # Every address the name resolves to, not just a literal IP in the URL: a name like
    # 127.0.0.1.nip.io, or a number like 2130706433, reaches a local address too.
    addresses = _resolve_host(hostname)
    if not addresses:
        raise ValueError(f"Endpoint URL's host {hostname} doesn't resolve")
    if any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("Endpoint URL must not target a private or local address")
    return value.rstrip("/")


def _resolve_host(hostname: str) -> list[str]:
    """Every IP address `hostname` resolves to (it may itself be an IP), or [] if none."""
    import socket

    try:
        return sorted({info[4][0].split("%")[0] for info in socket.getaddrinfo(hostname, 443)})
    except (OSError, UnicodeError):
        return []


def _get_fernet() -> Fernet:
    from agent.secret_box import fernet

    return fernet(BYOK_ENV_KEY, BYOK_KEY_FILE, b"coolton-byok-salt")


def _load_store() -> dict:
    return load_json(BYOK_STORE_FILE)


def _save_store(data: dict):
    save_json(BYOK_STORE_FILE, data, private=True)


def _enc(fernet: Fernet, value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def _dec(fernet: Fernet, encrypted: str) -> str:
    from agent.redact import add_runtime_secret

    value = fernet.decrypt(encrypted.encode()).decode()
    add_runtime_secret("BYOK_SECRET", value)
    return value


def _new_id() -> str:
    return "ep_" + uuid.uuid4().hex[:10]


def get_user_endpoints(user_id: str) -> list[dict]:
    with store_lock:
        data = _load_store()
        endpoints = data.get(user_id, {}).get("endpoints", {})
        result = []
        for ep_id, ep in endpoints.items():
            result.append(
                {
                    "id": ep_id,
                    "name": ep["name"],
                    "base_url": ep["base_url"],
                    "model": ep["model"],
                }
            )
        return result


def _get_endpoint_raw(user_id: str, ep_id: str) -> dict | None:
    with store_lock:
        data = _load_store()
        return data.get(user_id, {}).get("endpoints", {}).get(ep_id)


def get_endpoint_decrypted(user_id: str, ep_id: str) -> dict | None:
    ep = _get_endpoint_raw(user_id, ep_id)
    if not ep:
        return None
    fernet = _get_fernet()
    try:
        api_key = _dec(fernet, ep["api_key_encrypted"])
    except Exception:
        return None
    return {
        "id": ep_id,
        "name": ep["name"],
        "base_url": ep["base_url"],
        "api_key": api_key,
        "model": ep["model"],
    }


def add_endpoint(
    user_id: str, name: str, base_url: str, api_key: str, model: str
) -> str:
    fernet = _get_fernet()
    ep_id = _new_id()
    with store_lock:
        data = _load_store()
        user_data = data.setdefault(
            user_id,
            {"endpoints": {}, "text_endpoint_id": None, "image_endpoint_id": None},
        )
        endpoints = user_data.setdefault("endpoints", {})
        endpoints[ep_id] = {
            "name": name,
            "base_url": validate_endpoint_url(base_url),
            "api_key_encrypted": _enc(fernet, api_key),
            "model": model,
        }
        if user_data.get("text_endpoint_id") is None:
            user_data["text_endpoint_id"] = ep_id
        _save_store(data)
    return ep_id


def update_endpoint(
    user_id: str, ep_id: str, name: str, base_url: str, api_key: str, model: str
):
    fernet = _get_fernet()
    with store_lock:
        data = _load_store()
        ep = data.get(user_id, {}).get("endpoints", {}).get(ep_id)
        if not ep:
            raise ValueError(f"Endpoint {ep_id} not found")
        ep["name"] = name
        ep["base_url"] = validate_endpoint_url(base_url)
        if api_key and api_key != "••••••••":
            ep["api_key_encrypted"] = _enc(fernet, api_key)
        ep["model"] = model
        _save_store(data)


def delete_endpoint(user_id: str, ep_id: str):
    with store_lock:
        data = _load_store()
        user_data = data.get(user_id, {})
        user_data.get("endpoints", {}).pop(ep_id, None)
        if user_data.get("text_endpoint_id") == ep_id:
            remaining = list(user_data.get("endpoints", {}).keys())
            user_data["text_endpoint_id"] = remaining[0] if remaining else None
        if user_data.get("image_endpoint_id") == ep_id:
            remaining = list(user_data.get("endpoints", {}).keys())
            user_data["image_endpoint_id"] = remaining[0] if remaining else None
        _save_store(data)


def set_text_endpoint(user_id: str, ep_id: str | None):
    with store_lock:
        data = _load_store()
        user_data = data.setdefault(user_id, {})
        if ep_id is not None and ep_id not in user_data.get("endpoints", {}):
            raise ValueError(f"Endpoint {ep_id} not found")
        user_data["text_endpoint_id"] = ep_id
        _save_store(data)


def set_image_endpoint(user_id: str, ep_id: str | None):
    with store_lock:
        data = _load_store()
        user_data = data.setdefault(user_id, {})
        if ep_id is not None and ep_id not in user_data.get("endpoints", {}):
            raise ValueError(f"Endpoint {ep_id} not found")
        user_data["image_endpoint_id"] = ep_id
        _save_store(data)


def get_text_endpoint_id(user_id: str) -> str | None:
    with store_lock:
        data = _load_store()
        return data.get(user_id, {}).get("text_endpoint_id")


def get_image_endpoint_id(user_id: str) -> str | None:
    with store_lock:
        data = _load_store()
        return data.get(user_id, {}).get("image_endpoint_id")
