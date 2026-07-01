import os
import json
import base64
import logging
import threading
import uuid
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

BYOK_STORE_FILE = "byok_store.json"
BYOK_KEY_FILE = "byok_key.bin"
BYOK_ENV_KEY = "BYOK_ENCRYPTION_KEY"
store_lock = threading.Lock()


def _get_fernet() -> Fernet:
    key = os.environ.get(BYOK_ENV_KEY)
    if key:
        key_bytes = key.encode() if isinstance(key, str) else key
        if len(key_bytes) != 44:
            kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b"coolton-byok-salt", iterations=100000)
            key_bytes = base64.urlsafe_b64encode(kdf.derive(key_bytes))
        else:
            key_bytes = key_bytes if isinstance(key_bytes, bytes) else key_bytes.encode()
        return Fernet(key_bytes)

    if os.path.exists(BYOK_KEY_FILE):
        with open(BYOK_KEY_FILE, "rb") as f:
            return Fernet(f.read().strip())

    key = Fernet.generate_key()
    with open(BYOK_KEY_FILE, "wb") as f:
        f.write(key)
    logger.info("Generated new BYOK encryption key at %s", BYOK_KEY_FILE)
    return Fernet(key)


def _load_store() -> dict:
    if not os.path.exists(BYOK_STORE_FILE):
        return {}
    with open(BYOK_STORE_FILE, "r") as f:
        return json.load(f)


def _save_store(data: dict):
    temp = f"{BYOK_STORE_FILE}.tmp"
    with open(temp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(temp, BYOK_STORE_FILE)


def _enc(fernet: Fernet, value: str) -> str:
    return fernet.encrypt(value.encode()).decode()


def _dec(fernet: Fernet, encrypted: str) -> str:
    return fernet.decrypt(encrypted.encode()).decode()


def _new_id() -> str:
    return "ep_" + uuid.uuid4().hex[:10]


def get_user_endpoints(user_id: str) -> list[dict]:
    with store_lock:
        data = _load_store()
        endpoints = data.get(user_id, {}).get("endpoints", {})
        result = []
        for ep_id, ep in endpoints.items():
            result.append({
                "id": ep_id,
                "name": ep["name"],
                "base_url": ep["base_url"],
                "model": ep["model"],
            })
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


def add_endpoint(user_id: str, name: str, base_url: str, api_key: str, model: str) -> str:
    fernet = _get_fernet()
    ep_id = _new_id()
    with store_lock:
        data = _load_store()
        user_data = data.setdefault(user_id, {"endpoints": {}, "text_endpoint_id": None, "image_endpoint_id": None})
        endpoints = user_data.setdefault("endpoints", {})
        endpoints[ep_id] = {
            "name": name,
            "base_url": base_url.rstrip("/"),
            "api_key_encrypted": _enc(fernet, api_key),
            "model": model,
        }
        if user_data.get("text_endpoint_id") is None:
            user_data["text_endpoint_id"] = ep_id
        _save_store(data)
    return ep_id


def update_endpoint(user_id: str, ep_id: str, name: str, base_url: str, api_key: str, model: str):
    fernet = _get_fernet()
    with store_lock:
        data = _load_store()
        ep = data.get(user_id, {}).get("endpoints", {}).get(ep_id)
        if not ep:
            raise ValueError(f"Endpoint {ep_id} not found")
        ep["name"] = name
        ep["base_url"] = base_url.rstrip("/")
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
        data.setdefault(user_id, {})["text_endpoint_id"] = ep_id
        _save_store(data)


def set_image_endpoint(user_id: str, ep_id: str | None):
    with store_lock:
        data = _load_store()
        data.setdefault(user_id, {})["image_endpoint_id"] = ep_id
        _save_store(data)


def get_text_endpoint_id(user_id: str) -> str | None:
    with store_lock:
        data = _load_store()
        return data.get(user_id, {}).get("text_endpoint_id")


def get_image_endpoint_id(user_id: str) -> str | None:
    with store_lock:
        data = _load_store()
        return data.get(user_id, {}).get("image_endpoint_id")


def has_any_endpoint(user_id: str) -> bool:
    with store_lock:
        data = _load_store()
        return bool(data.get(user_id, {}).get("endpoints"))


def get_preferred_model(user_id: str) -> str | None:
    with store_lock:
        data = _load_store()
        return data.get(user_id, {}).get("preferred_model")


def set_preferred_model(user_id: str, model: str):
    with store_lock:
        data = _load_store()
        data.setdefault(user_id, {})["preferred_model"] = model
        _save_store(data)
