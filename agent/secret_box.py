"""Encryption at rest for the credentials users give coolton (BYOK API keys,
MCP server tokens and headers), shared by agent.byok_store and agent.mcp_server_store.

Each store has its own key: `env_var` if set (a Fernet key, or any passphrase,
stretched with PBKDF2 under the store's own `salt`), else a random key generated once
into `key_file` (owner-only). Derived keys are cached: PBKDF2 is deliberately slow,
and it used to run again on every single decrypt.
"""
from __future__ import annotations

import base64
import functools
import logging
import os
import threading

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

_key_file_lock = threading.Lock()


@functools.lru_cache(maxsize=16)
def _from_passphrase(passphrase: str, salt: bytes) -> Fernet:
    raw = passphrase.encode()
    if len(raw) == 44:  # already a urlsafe-base64 Fernet key
        return Fernet(raw)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100000)
    return Fernet(base64.urlsafe_b64encode(kdf.derive(raw)))


def fernet(env_var: str, key_file: str, salt: bytes) -> Fernet:
    passphrase = os.environ.get(env_var)
    if passphrase:
        return _from_passphrase(passphrase, salt)
    with _key_file_lock:
        try:
            with open(key_file, "rb") as f:
                return Fernet(f.read().strip())
        except FileNotFoundError:
            pass
        key = Fernet.generate_key()
        from agent.json_file import write_private_bytes

        write_private_bytes(key_file, key)
        logger.info("Generated a new encryption key at %s", key_file)
        return Fernet(key)
