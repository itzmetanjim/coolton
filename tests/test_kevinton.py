from unittest.mock import Mock, patch

import agent.kevinton as kevinton


def test_kevinton_enabled_defaults_true(monkeypatch):
    monkeypatch.delenv("KEVINTON_ENABLED", raising=False)
    assert kevinton.kevinton_enabled() is True


def test_kevinton_enabled_false_values(monkeypatch):
    for value in ("false", "False", "0", "no", "off", "  FALSE  "):
        monkeypatch.setenv("KEVINTON_ENABLED", value)
        assert kevinton.kevinton_enabled() is False, f"expected disabled for {value!r}"


def test_kevinton_enabled_true_values(monkeypatch):
    for value in ("true", "1", "yes", "anything-else"):
        monkeypatch.setenv("KEVINTON_ENABLED", value)
        assert kevinton.kevinton_enabled() is True, f"expected enabled for {value!r}"


def test_spawn_kevinton_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("KEVINTON_ENABLED", "false")
    with patch("agent.kevinton.threading.Thread") as thread_cls:
        kevinton.spawn_kevinton("hi", [], "C1", "1.1", deps=Mock())
        thread_cls.assert_not_called()


def test_spawn_kevinton_starts_thread_when_enabled(monkeypatch):
    monkeypatch.delenv("KEVINTON_ENABLED", raising=False)
    with patch("agent.kevinton.threading.Thread") as thread_cls:
        instance = thread_cls.return_value
        kevinton.spawn_kevinton("hi", [], "C1", "1.1", deps=Mock())
        thread_cls.assert_called_once()
        instance.start.assert_called_once()


def test_run_kevinton_marks_its_skill_changes_for_review(monkeypatch):
    """kevinton works from untrusted transcripts, so its skill changes always go
    to maintainer review (agent.skill_review) — without touching the finished
    turn's own deps object."""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from agent import kevinton

    seen = {}

    def fake_run_sync(**kwargs):
        seen["deps"] = kwargs["deps"]
        return SimpleNamespace(output="no skill needed")

    monkeypatch.setattr(kevinton, "build_kevinton_agent", lambda: (SimpleNamespace(run_sync=fake_run_sync), []))
    monkeypatch.setattr(kevinton, "_kevinton_model", lambda deps: "test-model")
    monkeypatch.setattr(kevinton, "get_thread_sandbox_id", lambda c, t: None)
    deps = SimpleNamespace(user_id="U1", channel_id="C1", client=Mock())

    assert kevinton.run_kevinton("hi", [], "C1", "1.1", deps) == "no skill needed"
    assert seen["deps"].skill_review_required is True
    assert not hasattr(deps, "skill_review_required")
