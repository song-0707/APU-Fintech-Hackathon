"""Regression tests for graph_builder's name normalization (_normalize_key)
and knowledge-triple entity-taxonomy labeling.

Normalization exists because Gemini/Vision-extracted names vary in
case/whitespace across meetings (e.g. "Sarah Park" vs "sarah park"), which
used to MERGE onto separate near-duplicate nodes. The taxonomy (EntityType)
exists because knowledge_triples subjects/objects used to all become a
single generic :Entity label — now each is written under whichever of
Person/Project/Organization/System/Policy/Document/Concept Gemini
classified it as, so e.g. a triple subject typed Person merges onto the
same node intelligence.participants already created.

Each test patches app.graph.graph_builder.run_query directly, not
app.graph.neo4j_service.run_query: graph_builder imports run_query by name
(`from app.graph.neo4j_service import run_query`), so the name callers
resolve at call time lives in graph_builder's own module namespace — unlike
api/graph.py, which accesses it via `neo4j_service.run_query(...)` and is
patched at the neo4j_service module instead (see test_graph_api.py)."""
from unittest.mock import MagicMock, patch

from app.graph import graph_builder
from app.schemas.meeting_intelligence import (
    ActionItem,
    Decision,
    DecisionConfidence,
    EntityType,
    KnowledgeTriple,
    MeetingIntelligence,
)


def _intelligence(**kwargs) -> MeetingIntelligence:
    return MeetingIntelligence(meeting_id="m1", **kwargs)


def test_normalize_key_collapses_whitespace_and_case():
    assert graph_builder._normalize_key("Sarah Park") == "sarah park"
    assert graph_builder._normalize_key("  Sarah   Park  ") == "sarah park"
    assert graph_builder._normalize_key("SARAH PARK") == "sarah park"


def test_participant_merge_uses_normalized_key_but_preserves_raw_name():
    mock_run = MagicMock(return_value=[])
    with patch.object(graph_builder, "run_query", mock_run):
        graph_builder.build_from_meeting("m1", "Title", None, _intelligence(participants=["  Sarah  Park "]))

    person_calls = [c for c in mock_run.call_args_list if "PARTICIPATED_IN" in c.args[0]]
    assert len(person_calls) == 1
    _, kwargs = person_calls[0]
    assert kwargs["key"] == "sarah park"
    assert kwargs["name"] == "  Sarah  Park "  # raw casing/spacing preserved for ON CREATE SET


def test_project_merge_uses_normalized_key():
    mock_run = MagicMock(return_value=[])
    with patch.object(graph_builder, "run_query", mock_run):
        graph_builder.build_from_meeting("m1", "Title", "  Project Phoenix ", _intelligence())

    project_calls = [
        c for c in mock_run.call_args_list
        if "RELATES_TO]->(pr)" in c.args[0] and "Meeting {id: $meeting_id})" in c.args[0]
    ]
    assert len(project_calls) == 1
    _, kwargs = project_calls[0]
    assert kwargs["project_key"] == "project phoenix"
    assert kwargs["project"] == "  Project Phoenix "


def test_decision_speaker_and_decision_project_merges_use_normalized_keys():
    mock_run = MagicMock(return_value=[])
    decision = Decision(
        text="Ship it", confidence=DecisionConfidence.firm_commitment, timestamp="00:00:00", speaker="ALEX chen"
    )
    with patch.object(graph_builder, "run_query", mock_run):
        graph_builder.build_from_meeting("m1", "Title", "Phoenix", _intelligence(decisions=[decision]))

    made_by_calls = [c for c in mock_run.call_args_list if "MADE_BY" in c.args[0]]
    assert len(made_by_calls) == 1
    _, kwargs = made_by_calls[0]
    assert kwargs["speaker_key"] == "alex chen"
    assert kwargs["speaker"] == "ALEX chen"

    decision_project_calls = [
        c for c in mock_run.call_args_list
        if c.args[0].startswith("MATCH (d:Decision {id: $id})") and "RELATES_TO" in c.args[0]
    ]
    assert len(decision_project_calls) == 1
    _, kwargs = decision_project_calls[0]
    assert kwargs["project_key"] == "phoenix"


def test_action_item_assignee_merge_uses_normalized_key():
    mock_run = MagicMock(return_value=[])
    item = ActionItem(task="Write report", assignee="  Tom Wright")
    with patch.object(graph_builder, "run_query", mock_run):
        graph_builder.build_from_meeting("m1", "Title", None, _intelligence(action_items=[item]))

    assigned_calls = [c for c in mock_run.call_args_list if "ASSIGNED_TO" in c.args[0]]
    assert len(assigned_calls) == 1
    _, kwargs = assigned_calls[0]
    assert kwargs["assignee_key"] == "tom wright"
    assert kwargs["assignee"] == "  Tom Wright"


def test_blank_decision_speaker_does_not_create_person_node():
    mock_run = MagicMock(return_value=[])
    decision = Decision(
        text="Ship it", confidence=DecisionConfidence.firm_commitment, timestamp="00:00:00", speaker=""
    )
    with patch.object(graph_builder, "run_query", mock_run):
        graph_builder.build_from_meeting("m1", "Title", None, _intelligence(decisions=[decision]))

    assert not [c for c in mock_run.call_args_list if "MADE_BY" in c.args[0]]


def test_blank_action_item_assignee_does_not_create_person_node():
    mock_run = MagicMock(return_value=[])
    item = ActionItem(task="Write report", assignee="")
    with patch.object(graph_builder, "run_query", mock_run):
        graph_builder.build_from_meeting("m1", "Title", None, _intelligence(action_items=[item]))

    assert not [c for c in mock_run.call_args_list if "ASSIGNED_TO" in c.args[0]]


def test_knowledge_triple_merge_uses_normalized_keys_for_subject_and_object():
    mock_run = MagicMock(return_value=[])
    triple = KnowledgeTriple(
        subject="Project Alpha", subject_type=EntityType.project,
        predicate="USES_VENDOR",
        object="  provider X", object_type=EntityType.organization,
    )
    with patch.object(graph_builder, "run_query", mock_run):
        graph_builder.build_from_meeting("m1", "Title", None, _intelligence(knowledge_triples=[triple]))

    triple_calls = [c for c in mock_run.call_args_list if "RELATES_AS" in c.args[0]]
    assert len(triple_calls) == 1
    cypher, kwargs = triple_calls[0]
    assert kwargs["subject_key"] == "project alpha"
    assert kwargs["object_key"] == "provider x"
    # The taxonomy type is interpolated as the actual Cypher label, not a
    # generic "Entity" — this is what lets a Person/Project-typed triple
    # land on the same node the participants/project loops already use.
    assert "MERGE (s:Project {key: $subject_key})" in cypher[0]
    assert "MERGE (o:Organization {key: $object_key})" in cypher[0]
    assert "SET r.meeting_ids = CASE" in cypher[0]
    assert kwargs["meeting_id"] == "m1"


def test_knowledge_triple_defaults_to_concept_when_type_unspecified():
    mock_run = MagicMock(return_value=[])
    triple = KnowledgeTriple(subject="Vendor concentration risk", predicate="RAISED_IN", object="Q2 All-Hands")
    with patch.object(graph_builder, "run_query", mock_run):
        graph_builder.build_from_meeting("m1", "Title", None, _intelligence(knowledge_triples=[triple]))

    cypher, _ = next(c for c in mock_run.call_args_list if "RELATES_AS" in c.args[0])
    assert "MERGE (s:Concept {key: $subject_key})" in cypher[0]
    assert "MERGE (o:Concept {key: $object_key})" in cypher[0]


def test_delete_meeting_removes_owned_knowledge_triple_edges_and_legacy_orphans():
    mock_run = MagicMock(return_value=[])
    with patch.object(graph_builder, "run_query", mock_run):
        graph_builder.delete_meeting("m1")

    statements = [c.args[0] for c in mock_run.call_args_list]
    assert any("MATCH ()-[r:RELATES_AS]->()" in stmt and "DELETE r" in stmt for stmt in statements)
    assert any("DETACH DELETE m, d, a" in stmt for stmt in statements)
    assert any("r.meeting_ids IS NULL" in stmt and "MENTIONED_IN" in stmt and "DELETE r" in stmt for stmt in statements)


def test_knowledge_triple_typed_as_person_uses_the_same_label_and_key_as_participants():
    """A triple subject Gemini classifies as Person must be indistinguishable,
    at the Cypher level, from an ordinary participant MERGE — that's what
    makes them land on the same Neo4j node instead of a Person/Entity
    near-duplicate pair."""
    mock_run = MagicMock(return_value=[])
    triple = KnowledgeTriple(
        subject="Tom Wright", subject_type=EntityType.person,
        predicate="OWNS",
        object="Provider X security audit",
    )
    with patch.object(graph_builder, "run_query", mock_run):
        graph_builder.build_from_meeting(
            "m1", "Title", None, _intelligence(participants=["Tom Wright"], knowledge_triples=[triple])
        )

    participant_cypher, participant_kwargs = next(c for c in mock_run.call_args_list if "PARTICIPATED_IN" in c.args[0])
    triple_cypher, triple_kwargs = next(c for c in mock_run.call_args_list if "RELATES_AS" in c.args[0])
    assert "MERGE (p:Person {key: $key})" in participant_cypher[0]
    assert "MERGE (s:Person {key: $subject_key})" in triple_cypher[0]
    assert participant_kwargs["key"] == triple_kwargs["subject_key"] == "tom wright"


def test_seed_demo_history_person_merges_use_normalized_key():
    mock_run = MagicMock(return_value=[])
    with patch.object(graph_builder, "run_query", mock_run):
        graph_builder.seed_demo_history()

    person_calls = [c for c in mock_run.call_args_list if "PARTICIPATED_IN" in c.args[0]]
    assert len(person_calls) == 2  # origin seed meeting + Q2 seed meeting
    for _, kwargs in person_calls:
        assert kwargs["person_key"] == "sarah park"


def test_ensure_constraints_drops_old_name_and_entity_constraints_and_creates_taxonomy_key_constraints():
    mock_run = MagicMock(return_value=[])
    with patch.object(graph_builder, "run_query", mock_run):
        graph_builder.ensure_constraints()

    statements = [c.args[0] for c in mock_run.call_args_list]
    assert "DROP CONSTRAINT person_name IF EXISTS" in statements
    assert "DROP CONSTRAINT project_name IF EXISTS" in statements
    assert "DROP CONSTRAINT entity_name IF EXISTS" in statements
    # entity_key existed only transiently, under the single-generic-label
    # design the taxonomy replaced — must be dropped, not (re-)created.
    assert "DROP CONSTRAINT entity_key IF EXISTS" in statements
    assert not any("CREATE CONSTRAINT entity_key" in s for s in statements)
    for label_key, prop in [
        ("person_key", "p.key"), ("project_key", "pr.key"), ("organization_key", "o.key"),
        ("system_key", "s.key"), ("policy_key", "p.key"), ("document_key", "d.key"), ("concept_key", "c.key"),
    ]:
        assert any(label_key in s and f"{prop} IS UNIQUE" in s for s in statements), f"missing constraint for {label_key}"
