"""agent.redact.secret_values(): parsing .env for secret-shaped keys, including
quoted values and an "export " prefix — a shell-sourceable .env is valid,
values are often quoted (VAR="abc"), and the bare secret is what actually
shows up in tool output/errors, so a quoted value must be unwrapped before
being registered or it would never match and never get redacted."""

import os

import pytest

from agent import redact


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(redact, "_ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(redact, "_XOXE_TOKENS_PATH", tmp_path / "xoxe_tokens.json")
    # secret_values() also pulls from the real process environment — clear any
    # secret-shaped var so these tests only see what each test's .env writes.
    for k in list(os.environ):
        if any(h in k for h in redact._SECRET_NAME_HINTS):
            monkeypatch.delenv(k, raising=False)
    redact.invalidate_secret_cache()
    yield
    redact.invalidate_secret_cache()


def _write_env(tmp_path, text):
    (tmp_path / ".env").write_text(text)


def test_unquoted_env_secret_is_redacted(tmp_path):
    _write_env(tmp_path, "SOME_API_KEY=sk-plainvalue\n")
    assert redact.redact("leaked sk-plainvalue here") == "leaked *** here"


def test_double_quoted_env_secret_is_unwrapped_and_redacted(tmp_path):
    _write_env(tmp_path, 'SOME_API_KEY="sk-quotedvalue"\n')
    assert redact.redact("leaked sk-quotedvalue here") == "leaked *** here"


def test_single_quoted_env_secret_is_unwrapped_and_redacted(tmp_path):
    _write_env(tmp_path, "SOME_API_KEY='sk-singlequoted'\n")
    assert redact.redact("leaked sk-singlequoted here") == "leaked *** here"


def test_export_prefixed_env_secret_is_redacted(tmp_path):
    _write_env(tmp_path, "export SOME_API_KEY=sk-exportedvalue\n")
    assert redact.redact("leaked sk-exportedvalue here") == "leaked *** here"


def test_export_and_quotes_together(tmp_path):
    _write_env(tmp_path, 'export SOME_API_KEY="sk-both"\n')
    assert redact.redact("leaked sk-both here") == "leaked *** here"


def test_whitespace_around_key_and_value_is_stripped(tmp_path):
    _write_env(tmp_path, "SOME_API_KEY = sk-spaced \n")
    assert redact.redact("leaked sk-spaced here") == "leaked *** here"


def test_non_secret_key_is_left_alone(tmp_path):
    _write_env(tmp_path, "SOME_OTHER_VAR=not-a-secret\n")
    assert redact.redact("mentions not-a-secret here") == "mentions not-a-secret here"
