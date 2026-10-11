import importlib
import subprocess
from unittest.mock import Mock


def test_the_footer_shows_the_running_commit(monkeypatch):
    commit = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(subprocess, "run", Mock(return_value=Mock(stdout=commit + "\n")))
    import agent.version as version
    importlib.reload(version)
    try:
        assert version.COMMIT_HASH == commit
        assert version.COMMIT_SHORT_HASH == "0123456789"
        assert version.COMMIT_URL == f"https://github.com/itzmetanjim/coolton/commit/{commit}"
    finally:
        monkeypatch.undo()
        importlib.reload(version)  # restore real state for any later test


def test_falls_back_gracefully_when_git_unavailable(monkeypatch):
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=FileNotFoundError("no git")))
    import agent.version as version
    importlib.reload(version)
    try:
        assert version.COMMIT_HASH == ""
        assert version.COMMIT_SHORT_HASH == "unknown"
        assert version.COMMIT_URL is None
    finally:
        importlib.reload(version)  # restore real state for any later test
