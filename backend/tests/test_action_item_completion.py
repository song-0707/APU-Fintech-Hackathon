"""Regression tests for action-item completion persistence: the Neo4j
`completed` field (graph_builder), the PATCH /action-items/{id} endpoint,
and /meeting/{id}/summary exposing each item's real id + current
completion status.

Reported gap: marking a task "Done" only ever updated frontend React
state — nothing backend-durable existed to read it back from, so a reload
silently undid it. See app/graph/graph_builder.py's action_item_id /
get_action_item / get_action_item_completions / set_action_item_completed,
and PATCH /action-items/{id} in app/api/graph.py.

Patches app.graph.graph_builder.run_query throughout, including for the
HTTP-endpoint tests below: PATCH /action-items/{id} and the summary
annotation both call graph_builder's own helpers (get_action_item,
set_action_item_completed, action_item_id, get_action_item_completions),
which resolve run_query by name in graph_builder's own module namespace —
unlike api/graph.py's other endpoints, which call
neo4j_service.run_query(...) by dotted access and so get patched at
neo4j_service instead (see test_graph_builder.py's and
test_graph_api.py's docstrings for the general rule)."""
import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.graph import graph_builder
from app.main import app
from app.models.employee import Employee
from app.schemas.meeting_intelligence import ActionItem, MeetingIntelligence

client = TestClient(app, raise_server_exceptions=False)


def test_action_item_id_is_deterministic_and_meeting_task_scoped():
    """meetings.py's /summary endpoint computes this independently of
    Neo4j — it must land on the exact id build_from_meeting used when it
    created the node, or the two can never agree on identity."""
    a = graph_builder.action_item_id("mtg-1", "Submit the budget")
    b = graph_builder.action_item_id("mtg-1", "Submit the budget")
    assert a == b
    assert a != graph_builder.action_item_id("mtg-1", "A different task")
    assert a != graph_builder.action_item_id("mtg-2", "Submit the budget")


def test_build_from_meeting_defaults_completed_false_only_on_create():
    """ON CREATE SET, not a plain SET — reprocessing the same meeting must
    never reset an already-completed task back to false."""
    intelligence = MeetingIntelligence(
        meeting_id="mtg-1",
        action_items=[ActionItem(task="Do the thing", assignee="Alex", deadline=None, priority="medium")],
    )
    with patch("app.graph.graph_builder.run_query") as mock_run_query:
        graph_builder.build_from_meeting("mtg-1", "Sync", None, intelligence)

    action_item_calls = [c for c in mock_run_query.call_args_list if "ActionItem" in c.args[0]]
    assert len(action_item_calls) == 1
    cypher = action_item_calls[0].args[0]
    assert "MERGE (a:ActionItem {id: $id})" in cypher
    assert "ON CREATE SET a.completed = false" in cypher
    assert "SET a.task = $task" in cypher


def test_get_action_item_returns_none_when_not_found():
    with patch("app.graph.graph_builder.run_query", return_value=[]):
        assert graph_builder.get_action_item("missing-id") is None


def test_get_action_item_returns_task_assignee_completed():
    with patch(
        "app.graph.graph_builder.run_query",
        return_value=[{"task": "Do the thing", "assignee": "Alex", "completed": True}],
    ):
        item = graph_builder.get_action_item("item-1")
    assert item == {"task": "Do the thing", "assignee": "Alex", "completed": True}


def test_get_action_item_completions_maps_id_to_completed():
    with patch(
        "app.graph.graph_builder.run_query",
        return_value=[{"id": "a1", "completed": True}, {"id": "a2", "completed": False}],
    ):
        completions = graph_builder.get_action_item_completions("mtg-1")
    assert completions == {"a1": True, "a2": False}


def test_set_action_item_completed_issues_parameterized_update():
    with patch("app.graph.graph_builder.run_query") as mock_run_query:
        graph_builder.set_action_item_completed("item-1", True)
    mock_run_query.assert_called_once_with(
        "MATCH (a:ActionItem {id: $id}) SET a.completed = $completed",
        id="item-1",
        completed=True,
    )


# ── PATCH /action-items/{item_id} ────────────────────────────────────────

def test_patch_action_item_assignee_can_complete_own_task(db_session):
    employee = Employee(name="Alex Assignee", email="alex.assignee@corpbrain.ai", is_management=False)
    db_session.add(employee)
    db_session.commit()

    with patch(
        "app.graph.graph_builder.run_query",
        side_effect=[
            [{"task": "Do the thing", "assignee": "Alex Assignee", "completed": False}],  # get_action_item
            [],  # set_action_item_completed
        ],
    ):
        response = client.patch(
            "/action-items/item-1",
            json={"completed": True},
            headers={"X-User-Name": "Alex Assignee"},
        )

    assert response.status_code == 200
    assert response.json() == {"id": "item-1", "completed": True}


def test_patch_action_item_management_can_complete_anyone_elses_task(db_session, management_employee, caller_headers):
    with patch(
        "app.graph.graph_builder.run_query",
        side_effect=[
            [{"task": "Do the thing", "assignee": "Someone Else", "completed": False}],
            [],
        ],
    ):
        response = client.patch("/action-items/item-1", json={"completed": True}, headers=caller_headers)

    assert response.status_code == 200


def test_patch_action_item_rejects_non_assignee_non_management(db_session):
    outsider = Employee(name="Outsider Employee", email="outsider.item@corpbrain.ai", is_management=False)
    db_session.add(outsider)
    db_session.commit()

    with patch(
        "app.graph.graph_builder.run_query",
        return_value=[{"task": "Do the thing", "assignee": "Someone Else", "completed": False}],
    ):
        response = client.patch(
            "/action-items/item-1",
            json={"completed": True},
            headers={"X-User-Name": "Outsider Employee"},
        )

    assert response.status_code == 403


def test_patch_action_item_404s_for_unknown_id(db_session, management_employee, caller_headers):
    with patch("app.graph.graph_builder.run_query", return_value=[]):
        response = client.patch("/action-items/does-not-exist", json={"completed": True}, headers=caller_headers)

    assert response.status_code == 404


# ── GET /meeting/{id}/summary annotates action items ─────────────────────

def test_meeting_summary_annotates_action_items_with_id_and_completed(
    db_session, management_employee, caller_headers, tmp_path, monkeypatch
):
    from app.models.meeting import Meeting
    from app.services.storage_service import StorageService

    meeting = Meeting(id="mtg-summary", title="Sync", status="completed")
    db_session.add(meeting)
    db_session.commit()

    storage = StorageService(base_path=str(tmp_path))
    monkeypatch.setattr("app.api.meetings.storage", storage)
    storage.save_summary("mtg-summary", {
        "summary": "...",
        "decisions": [],
        "action_items": [
            {"task": "Do the thing", "assignee": "Alex", "deadline": None, "priority": "medium"},
        ],
    })

    expected_id = graph_builder.action_item_id("mtg-summary", "Do the thing")
    with patch("app.graph.graph_builder.run_query", return_value=[{"id": expected_id, "completed": True}]):
        response = client.get("/meeting/mtg-summary/summary", headers=caller_headers)

    assert response.status_code == 200
    item = response.json()["action_items"][0]
    assert item["id"] == expected_id
    assert item["completed"] is True


def test_meeting_summary_defaults_completed_false_for_item_missing_from_graph(
    db_session, management_employee, caller_headers, tmp_path, monkeypatch
):
    """A task added to the stored summary since the last graph write (or a
    meeting whose graph write hasn't happened yet) must not crash or show
    as completed just because it's absent from get_action_item_completions'
    result — it should read as not-done, the same as any fresh task."""
    from app.models.meeting import Meeting
    from app.services.storage_service import StorageService

    meeting = Meeting(id="mtg-summary-2", title="Sync", status="completed")
    db_session.add(meeting)
    db_session.commit()

    storage = StorageService(base_path=str(tmp_path))
    monkeypatch.setattr("app.api.meetings.storage", storage)
    storage.save_summary("mtg-summary-2", {
        "summary": "...",
        "decisions": [],
        "action_items": [
            {"task": "Untracked in graph yet", "assignee": "Alex", "deadline": None, "priority": "medium"},
        ],
    })

    with patch("app.graph.graph_builder.run_query", return_value=[]):
        response = client.get("/meeting/mtg-summary-2/summary", headers=caller_headers)

    assert response.status_code == 200
    item = response.json()["action_items"][0]
    assert item["completed"] is False
