from types import SimpleNamespace

import pytest

from agent import thread_rip
from listeners.events import shared


@pytest.mark.parametrize("text,expected", [
    ("!threadrip", (True, None)),
    ("<@UBOT> !threadrip", (True, None)),
    ("!threadrip <https://x.slack.com/archives/C1/p1794054476315909>",
     (True, "https://x.slack.com/archives/C1/p1794054476315909")),
    ("please don't !threadrip this", (False, None)),
    ("!threadripper", (False, None)),
])
def test_only_the_whole_message_is_the_command(text, expected):
    assert thread_rip.parse_threadrip(text, "UBOT") == expected


def test_a_link_names_its_thread():
    assert thread_rip.thread_from_link("https://x.slack.com/archives/C1/p1794054476315909") == ("C1", "1794054476.315909")
    reply = "https://x.slack.com/archives/C1/p1794054500000001?thread_ts=1794054476.315909&cid=C1"
    assert thread_rip.thread_from_link(reply) == ("C1", "1794054476.315909")
    assert thread_rip.thread_from_link("not a link") is None


def test_only_coolton_messages_are_deleted_parent_last(monkeypatch):
    deleted = []

    class Deleter:
        def __init__(self, who):
            self.who = who

        def chat_delete(self, channel, ts):
            deleted.append((self.who, ts))

    monkeypatch.setattr(thread_rip, "_identities", lambda: {"UBOT": Deleter("bot"), "UCOOL": Deleter("user")})
    monkeypatch.setattr(thread_rip, "_replies", lambda reader, channel, ts: [
        {"ts": "1.0", "thread_ts": "1.0", "user": "UBOT"},
        {"ts": "2.0", "thread_ts": "1.0", "user": "UHUMAN"},
        {"ts": "3.0", "thread_ts": "1.0", "user": "UCOOL"},
        {"ts": "4.0", "thread_ts": "1.0", "user": "UBOT"},
    ])
    monkeypatch.setattr(thread_rip.time, "sleep", lambda s: None)
    monkeypatch.setattr("agent.active_runs.is_run_active", lambda c, t: False)

    assert thread_rip.rip_thread("C1", "1.0") == (3, [])
    assert deleted == [("user", "3.0"), ("bot", "4.0"), ("bot", "1.0")]


def test_anyone_but_the_admin_is_ignored_silently(monkeypatch):
    ran = []
    monkeypatch.setattr(shared, "is_authorized", lambda user: user == "UADMIN")
    monkeypatch.setattr(thread_rip, "handle_threadrip", lambda *a: ran.append(a))
    started = []
    monkeypatch.setattr("threading.Thread", lambda target, args, **kw: SimpleNamespace(
        start=lambda: started.append(target(*args))))

    event = {"thread_ts": "1.0", "ts": "2.0"}
    assert shared.handled_threadrip_command(None, "!threadrip", "UBOT", "UOTHER", "C1", event)
    assert ran == []
    assert shared.handled_threadrip_command(None, "!threadrip", "UBOT", "UADMIN", "C1", event)
    assert ran[0][1:] == (None, "UADMIN", "C1", "1.0", "1.0")
    assert not shared.handled_threadrip_command(None, "hello", "UBOT", "UADMIN", "C1", event)
