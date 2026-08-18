"""Regression tests for the /graph and /meeting/{id}/graph-data endpoints:
every link's source/target must correspond to a node actually present in
the same response's nodes array, or react-force-graph-2d's d3-force layer
throws "node not found" and the frontend Memory Graph fails to render.

Each fixture below mocks app.graph.neo4j_service.run_query with a
side_effect list matching the exact, fixed order the endpoint issues its
Cypher queries in. The CONTRADICTS row deliberately references a decision
id ("d2") absent from the earlier decisions-query fixture, reproducing:

  - get_meeting_graph_data: a CONTRADICTS edge always points at "other", a
    Decision that may belong to a different meeting than the one this
    endpoint scopes its own decision-gathering query to — so it's *never*
    in `nodes` unless added explicitly.
  - get_global_graph_data: each Cypher query runs as its own independent
    Neo4j auto-commit transaction (see neo4j_service.run_query), so a
    decision + CONTRADICTS edge committed by a concurrent meeting-processing
    task between the "decisions" query and the "contradicts" query would be
    picked up by the latter but missed by the former.
"""
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.models.employee import Employee, MeetingParticipant

client = TestClient(app, raise_server_exceptions=False)


def _assert_no_dangling_links(payload: dict) -> None:
    node_ids = {n["id"] for n in payload["nodes"]}
    dangling = [
        link for link in payload["links"]
        if link["source"] not in node_ids or link["target"] not in node_ids
    ]
    assert not dangling, f"link(s) reference a node missing from nodes[]: {dangling}"


def test_global_graph_data_has_no_dangling_links_under_concurrent_write(db_session, caller_headers):
    responses = [
        [{"id": "m1", "title": "Meeting One"}],                      # meetings
        [],                                                            # PARTICIPATED_IN
        [{"id": "d1", "text": "Decision One", "meeting_id": "m1", "speaker": None}],  # decisions
        [],                                                            # action items
        [],                                                            # meeting-RELATES_TO-project
        [],                                                            # decision-RELATES_TO-project
        [],                                                            # taxonomy nodes MENTIONED_IN
        [],                                                            # taxonomy RELATES_AS
        [{"from_id": "d1", "from_text": "Decision One", "to_id": "d2",
          "to_text": "Decision Two (written concurrently)", "message": "conflict"}],  # CONTRADICTS
    ]
    # Management caller + no `person` param -> unscoped org-wide view,
    # unchanged from before access control existed (see _resolve_target).
    with patch("app.graph.neo4j_service.run_query", side_effect=responses):
        response = client.get("/graph", headers=caller_headers)

    assert response.status_code == 200
    payload = response.json()
    _assert_no_dangling_links(payload)
    assert {"decision:d1", "decision:d2"} <= {n["id"] for n in payload["nodes"]}


def test_global_graph_data_surfaces_taxonomy_nodes_with_predicate_as_link_type(db_session, caller_headers):
    """Organization/Project etc. nodes (from knowledge_triples) must come
    through as their real label, not a generic "Entity" bucket, and a
    RELATES_AS edge's displayed type is the predicate text, not the literal
    relationship type — see the rationale comment in api/graph.py."""
    responses = [
        [],  # meetings
        [],  # PARTICIPATED_IN
        [],  # decisions
        [],  # action items
        [],  # meeting-RELATES_TO-project
        [],  # decision-RELATES_TO-project
        [{"name": "Project Alpha", "label": "Project Alpha", "node_type": "Project", "meeting_id": "m1"}],
        [{"subject": "Project Alpha", "subject_label": "Project Alpha", "subject_type": "Project",
          "object": "Provider X", "object_label": "Provider X", "object_type": "Organization",
          "predicate": "USES_VENDOR"}],
        [],  # CONTRADICTS
    ]
    with patch("app.graph.neo4j_service.run_query", side_effect=responses):
        response = client.get("/graph", headers=caller_headers)

    assert response.status_code == 200
    payload = response.json()
    _assert_no_dangling_links(payload)
    nodes_by_id = {n["id"]: n for n in payload["nodes"]}
    assert nodes_by_id["project:Project Alpha"]["type"] == "Project"
    assert nodes_by_id["organization:Provider X"]["type"] == "Organization"
    triple_link = next(l for l in payload["links"] if l["source"] == "project:Project Alpha")
    assert triple_link["target"] == "organization:Provider X"
    assert triple_link["type"] == "USES_VENDOR"


def test_meeting_graph_data_has_no_dangling_links_for_cross_meeting_contradiction(db_session, caller_headers):
    responses = [
        [{"id": "m1", "title": "Meeting One"}],                      # meeting lookup
        [],                                                            # participants
        [{"id": "d1", "text": "Decision One", "speaker": None}],       # decisions (this meeting only)
        [],                                                            # action items
        [],                                                            # meeting-RELATES_TO-project
        [],                                                            # decision-RELATES_TO-project
        [],                                                            # taxonomy nodes MENTIONED_IN
        [],                                                            # taxonomy RELATES_AS
        [{"from_id": "d1", "to_id": "d2", "to_text": "External Decision", "message": "conflict"}],  # CONTRADICTS
    ]
    # Management caller so require_meeting_access short-circuits without
    # needing a seeded MeetingParticipant row — this test is about link
    # integrity, not access control (see the boundary tests below for that).
    with patch("app.graph.neo4j_service.run_query", side_effect=responses):
        response = client.get("/meeting/m1/graph-data", headers=caller_headers)

    assert response.status_code == 200
    payload = response.json()
    _assert_no_dangling_links(payload)
    assert {"decision:d1", "decision:d2"} <= {n["id"] for n in payload["nodes"]}


# ── Access control boundary (the audit's core finding: this was untested) ──

def test_global_graph_requires_identity_header():
    response = client.get("/graph")
    assert response.status_code == 401


def test_meeting_graph_data_rejects_non_participant(db_session):
    outsider = Employee(name="Outsider Employee", email="outsider@corpbrain.ai", is_management=False)
    db_session.add(outsider)
    db_session.commit()

    with patch("app.graph.neo4j_service.run_query", return_value=[{"id": "m1", "title": "Meeting One"}]):
        response = client.get(
            "/meeting/m1/graph-data", headers={"X-User-Name": "Outsider Employee"}
        )

    assert response.status_code == 403


def test_global_graph_scopes_to_callers_own_meetings(db_session):
    """No `person` param + a non-management caller -> only their own
    meetings, sourced from meeting_participants (not a Person-name match in
    Neo4j) — the direct fix for the audit's full-org-fallback finding."""
    employee = Employee(name="Scoped Employee", email="scoped.employee@corpbrain.ai", is_management=False)
    db_session.add(employee)
    db_session.commit()
    db_session.add(MeetingParticipant(meeting_id="m1", employee_id=employee.id))
    db_session.commit()

    responses = [
        [{"id": "m1", "title": "Meeting One"}, {"id": "m2", "title": "Meeting Two"}],  # meetings
        [{"name": "Scoped Employee", "label": "Scoped Employee", "meeting_id": "m1"}],  # PARTICIPATED_IN
        [],  # decisions
        [],  # action items
        [],  # meeting-RELATES_TO-project
        [],  # decision-RELATES_TO-project
        [],  # taxonomy nodes
        [],  # taxonomy RELATES_AS
        [],  # CONTRADICTS
    ]
    with patch("app.graph.neo4j_service.run_query", side_effect=responses):
        response = client.get("/graph", headers={"X-User-Name": "Scoped Employee"})

    assert response.status_code == 200
    node_ids = {n["id"] for n in response.json()["nodes"]}
    assert "meeting:m1" in node_ids
    assert "meeting:m2" not in node_ids


def test_global_graph_with_no_meetings_is_empty_not_org_wide(db_session):
    """A recognized employee who isn't a participant of anything gets an
    empty graph — never the old "fall back to the full org graph" behavior
    this replaced."""
    employee = Employee(name="New Hire", email="new.hire@corpbrain.ai", is_management=False)
    db_session.add(employee)
    db_session.commit()

    responses = [
        [{"id": "m1", "title": "Meeting One"}],  # meetings
        [],  # PARTICIPATED_IN
        [], [], [], [], [], [],  # decisions/action items/relates-to x2/taxonomy x2
        [],  # CONTRADICTS
    ]
    with patch("app.graph.neo4j_service.run_query", side_effect=responses):
        response = client.get("/graph", headers={"X-User-Name": "New Hire"})

    assert response.status_code == 200
    assert response.json() == {"nodes": [], "links": []}
