"""Route-level behaviour for renaming and deleting a web conversation.

Routes are called directly with a stub request (the same shape
tests/test_web_auth.py uses) rather than through a TestClient, so these stay
about the routes' own logic and not about FastAPI's wiring.
"""

from unittest.mock import Mock, patch

import pytest
from fastapi import HTTPException

from web import conversation_log as log
from web import conversations as routes


@pytest.fixture(autouse=True)
def tmp_store(monkeypatch, tmp_path):
    monkeypatch.setattr(log, "STORE_DIR", str(tmp_path / "web_conversations"))
    log._locks.clear()
    log._last_seq.clear()
    with log._subscribers_guard:
        log._subscribers.clear()


def _request_for(slack_id):
    request = Mock()
    request.cookies = {}
    return request, patch.object(routes, "require_slack_id", return_value=slack_id)


def test_list_conversations_reports_working_from_active_runs():
    """The sidebar dot for a session you aren't currently viewing has no SSE
    stream to tell it a turn landed — it has to poll this field instead."""
    import time

    from agent.active_runs import mark_run_finished, mark_run_started

    busy = log.create_conversation("U1", title="busy")
    idle = log.create_conversation("U1", title="idle")
    mark_run_started("web", busy, time.time())
    try:
        request, as_user = _request_for("U1")
        with as_user:
            rows = routes.list_conversations_route(request)
        by_id = {r["id"]: r for r in rows}
        assert by_id[busy]["working"] is True
        assert by_id[idle]["working"] is False
    finally:
        mark_run_finished("web", busy)


def test_rename_stores_the_new_title():
    cid = log.create_conversation("U1", title="old")
    request, as_user = _request_for("U1")
    with as_user:
        routes.rename_conversation_route(cid, routes.RenameBody(title="a better name"), request)
    assert log.get_conversation_meta(cid)["title"] == "a better name"


def test_rename_collapses_whitespace_and_caps_length():
    cid = log.create_conversation("U1")
    request, as_user = _request_for("U1")
    with as_user:
        routes.rename_conversation_route(cid, routes.RenameBody(title="  a   b \n c  "), request)
        assert log.get_conversation_meta(cid)["title"] == "a b c"
        routes.rename_conversation_route(cid, routes.RenameBody(title="x" * 200), request)
    assert log.get_conversation_meta(cid)["title"] == "x" * 80


def test_rename_refuses_a_conversation_someone_else_owns():
    cid = log.create_conversation("U1", title="mine")
    request, as_other = _request_for("U2")
    with as_other, pytest.raises(HTTPException) as exc:
        routes.rename_conversation_route(cid, routes.RenameBody(title="theirs"), request)
    assert exc.value.status_code == 404
    assert log.get_conversation_meta(cid)["title"] == "mine"


def test_delete_removes_the_conversation():
    cid = log.create_conversation("U1", title="doomed")
    request, as_user = _request_for("U1")
    with as_user:
        routes.delete_conversation_route(cid, request)
    assert log.get_conversation_meta(cid) is None


def test_delete_stops_a_turn_still_running_in_that_conversation():
    """Otherwise the run keeps appending events to a log nobody can reach."""
    cid = log.create_conversation("U1")
    request, as_user = _request_for("U1")
    with as_user, patch("agent.stop_store.request_stop") as request_stop:
        routes.delete_conversation_route(cid, request)
    request_stop.assert_called_once_with("web", cid)


def test_delete_refuses_a_conversation_someone_else_owns():
    cid = log.create_conversation("U1", title="mine")
    request, as_other = _request_for("U2")
    with as_other, pytest.raises(HTTPException) as exc:
        routes.delete_conversation_route(cid, request)
    assert exc.value.status_code == 404
    assert log.get_conversation_meta(cid) is not None
