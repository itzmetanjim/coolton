"""agent.secret_box: the at-rest encryption for user-supplied credentials."""
import os

from agent import secret_box


def test_a_passphrase_key_is_derived_once_and_round_trips(monkeypatch):
    monkeypatch.setenv("TEST_BOX_KEY", "not a fernet key, a passphrase")
    first = secret_box.fernet("TEST_BOX_KEY", "unused.bin", b"salt-a")
    assert secret_box.fernet("TEST_BOX_KEY", "unused.bin", b"salt-a") is first  # PBKDF2 not rerun
    assert first.decrypt(first.encrypt(b"sk-123")) == b"sk-123"
    other_store = secret_box.fernet("TEST_BOX_KEY", "unused.bin", b"salt-b")
    assert other_store.decrypt(other_store.encrypt(b"x")) == b"x" and other_store is not first


def test_without_a_passphrase_a_private_key_file_is_made_once(monkeypatch, tmp_path):
    monkeypatch.delenv("TEST_BOX_KEY", raising=False)
    key_file = tmp_path / "box.bin"
    box = secret_box.fernet("TEST_BOX_KEY", str(key_file), b"salt")
    again = secret_box.fernet("TEST_BOX_KEY", str(key_file), b"salt")
    assert again.decrypt(box.encrypt(b"tok")) == b"tok"
    assert oct(os.stat(key_file).st_mode & 0o777) == "0o600"
