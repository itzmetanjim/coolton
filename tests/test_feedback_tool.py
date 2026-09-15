from agent.tools import feedback


def test_submit_feedback_notifies_admin(monkeypatch):
    calls = []
    monkeypatch.setattr(feedback, "notify_admin", lambda text, **k: calls.append(text))

    result = feedback.submit_feedback("U1", "C1", "bug", "the wait tool never resumed the thread")

    assert "Logged bug feedback" in result
    assert len(calls) == 1
    assert "U1" in calls[0]
    assert "C1" in calls[0]
    assert "the wait tool never resumed the thread" in calls[0]


def test_submit_feedback_rejects_unknown_kind(monkeypatch):
    calls = []
    monkeypatch.setattr(feedback, "notify_admin", lambda text, **k: calls.append(text))
    result = feedback.submit_feedback("U1", "C1", "not-a-kind", "body")
    assert result.startswith("Error:")
    assert calls == []


def test_submit_feedback_rejects_empty_body(monkeypatch):
    calls = []
    monkeypatch.setattr(feedback, "notify_admin", lambda text, **k: calls.append(text))
    assert feedback.submit_feedback("U1", "C1", "praise", "  ").startswith("Error:")
    assert feedback.submit_feedback("U1", "C1", "praise", "").startswith("Error:")
    assert calls == []


def test_submit_feedback_kind_is_case_insensitive(monkeypatch):
    monkeypatch.setattr(feedback, "notify_admin", lambda text, **k: None)
    assert feedback.submit_feedback("U1", "C1", "BUG", "body").startswith("Logged bug")
