import os

import pytest
from cryptography.fernet import Fernet

from agent import byok_store as store


@pytest.fixture
def tmp_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "BYOK_STORE_FILE", str(tmp_path / "byok_store.json"))
    monkeypatch.setattr(store, "BYOK_KEY_FILE", str(tmp_path / "byok_key.bin"))
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", Fernet.generate_key().decode())
    return tmp_path


def test_add_endpoint_sets_text_default(tmp_store):
    ep_id = store.add_endpoint("U1", "My OpenAI", "https://api.openai.com/v1/", "sk-test", "gpt-4o")
    assert ep_id.startswith("ep_")
    assert store.get_text_endpoint_id("U1") == ep_id
    # trailing slash stripped
    ep = store.get_endpoint_decrypted("U1", ep_id)
    assert ep["base_url"] == "https://api.openai.com/v1"


def test_endpoint_encryption_roundtrip(tmp_store):
    ep_id = store.add_endpoint("U1", "n", "https://x", "super-secret-key", "m")
    ep = store.get_endpoint_decrypted("U1", ep_id)
    assert ep["api_key"] == "super-secret-key"


def test_get_user_endpoints_hides_secrets(tmp_store):
    store.add_endpoint("U1", "n", "https://x", "sk-secret", "m")
    endpoints = store.get_user_endpoints("U1")
    assert len(endpoints) == 1
    assert "api_key" not in endpoints[0]
    assert endpoints[0]["name"] == "n"


def test_get_endpoint_decrypted_unknown_returns_none(tmp_store):
    assert store.get_endpoint_decrypted("U1", "ep_unknown") is None


def test_update_endpoint_keeps_key_when_masked(tmp_store):
    ep_id = store.add_endpoint("U1", "n", "https://x", "original-key", "m1")
    store.update_endpoint("U1", ep_id, "new name", "https://y", "••••••••", "m2")
    ep = store.get_endpoint_decrypted("U1", ep_id)
    assert ep["name"] == "new name"
    assert ep["api_key"] == "original-key"
    assert ep["model"] == "m2"


def test_update_endpoint_replaces_key(tmp_store):
    ep_id = store.add_endpoint("U1", "n", "https://x", "old-key", "m")
    store.update_endpoint("U1", ep_id, "n", "https://x", "new-key", "m")
    assert store.get_endpoint_decrypted("U1", ep_id)["api_key"] == "new-key"


def test_update_endpoint_unknown_raises(tmp_store):
    with pytest.raises(ValueError):
        store.update_endpoint("U1", "ep_missing", "n", "https://x", "k", "m")


def test_delete_endpoint_promotes_remaining(tmp_store):
    ep1 = store.add_endpoint("U1", "a", "https://1", "k1", "m1")
    ep2 = store.add_endpoint("U1", "b", "https://2", "k2", "m2")
    store.delete_endpoint("U1", ep1)
    assert store.get_endpoint_decrypted("U1", ep1) is None
    assert store.get_text_endpoint_id("U1") == ep2


def test_delete_all_endpoints_clears_default(tmp_store):
    ep1 = store.add_endpoint("U1", "a", "https://1", "k1", "m1")
    store.delete_endpoint("U1", ep1)
    assert store.get_text_endpoint_id("U1") is None


def test_set_text_and_image_endpoints(tmp_store):
    ep = store.add_endpoint("U1", "a", "https://1", "k1", "m1")
    store.set_image_endpoint("U1", ep)
    store.set_text_endpoint("U1", None)
    assert store.get_image_endpoint_id("U1") == ep
    assert store.get_text_endpoint_id("U1") is None
    assert store.get_image_endpoint_id("Uunknown") is None


def test_key_generated_when_no_env(tmp_store, monkeypatch, tmp_path):
    monkeypatch.delenv("BYOK_ENCRYPTION_KEY", raising=False)
    # fresh paths not used by earlier tests
    monkeypatch.setattr(store, "BYOK_KEY_FILE", str(tmp_path / "fresh_key.bin"))
    assert not os.path.exists(store.BYOK_KEY_FILE)
    ep_id = store.add_endpoint("U1", "a", "https://1", "k1", "m1")
    assert store.get_endpoint_decrypted("U1", ep_id)["api_key"] == "k1"
    assert os.path.exists(store.BYOK_KEY_FILE)


def test_key_from_file_used_when_env_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("BYOK_ENCRYPTION_KEY", raising=False)
    key = Fernet.generate_key()
    (tmp_path / "byok_key.bin").write_bytes(key)
    monkeypatch.setattr(store, "BYOK_KEY_FILE", str(tmp_path / "byok_key.bin"))
    monkeypatch.setattr(store, "BYOK_STORE_FILE", str(tmp_path / "byok_store.json"))

    ep_id = store.add_endpoint("U1", "a", "https://1", "k1", "m1")
    assert store.get_endpoint_decrypted("U1", ep_id)["api_key"] == "k1"
