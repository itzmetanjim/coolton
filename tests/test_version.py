import importlib
import subprocess
from unittest.mock import Mock


def test_commit_hash_resolves_from_real_git_repo():
    import agent.version as version
    assert version.COMMIT_HASH
    assert version.COMMIT_SHORT_HASH == version.COMMIT_HASH[:10]
    assert version.COMMIT_URL == f"{version.REPO_URL}/commit/{version.COMMIT_HASH}"


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
