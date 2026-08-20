"""Graph schema (Task 4.2) + builder (Task 4.3).

Nodes: Meeting, Person, Decision, ActionItem, Project, plus a closed
taxonomy of knowledge_triples subject/object node labels — Organization,
System, Policy, Document, Concept (see
app.schemas.meeting_intelligence.EntityType) — instead of one generic
"Entity" label. A triple typed Person/Project MERGEs onto the exact same
node participants/project already use, not a separate one; Concept is the
fallback for anything that doesn't fit Organization/System/Policy/Document.
The predicate itself stays unbounded LLM text (see RELATES_AS below), but
the node label doesn't — an unbounded label per mention is what makes a
knowledge graph unreadable, so extraction is forced to pick from this fixed
list (see the prompt in gemini_service.run_gemini_analysis).

Relationships: PARTICIPATED_IN (Person->Meeting), MADE_IN (Decision/
ActionItem->Meeting), MADE_BY (Decision->Person), ASSIGNED_TO (ActionItem->
Person), RELATES_TO (Meeting/Decision->Project), CONTRADICTS (Decision->
Decision, written by contradiction_service), VIOLATES (Decision->Policy,
schema-only for now, not written anywhere), RELATES_AS (any taxonomy
label->any taxonomy label, the knowledge_triples predicate — kept as a
fixed relationship type with the actual predicate text as an edge property,
not a dynamic relationship type, since the predicate is unsanitized LLM
output and Neo4j relationship types can't be parameterized), MENTIONED_IN
(any taxonomy label->Meeting).
"""
import logging
import uuid

from app.graph.neo4j_service import run_query
from app.schemas.meeting_intelligence import MeetingIntelligence

logger = logging.getLogger(__name__)


def ensure_constraints() -> None:
    """Uniqueness constraints — idempotent, safe to call on every startup.

    Person/Project/Entity are constrained on `key` (see `_normalize_key`),
    not `name` — a case/whitespace-insensitive form of the name, so e.g.
    "Sarah Park" and "sarah park" MERGE onto one node instead of creating
    near-duplicates. The old `*_name` constraints are dropped first so a
    database created before this change converges onto the new `*_key`
    ones instead of enforcing both. Dropping/recreating a constraint is a
    schema-only operation — it does not touch existing data, so any nodes
    already duplicated under the old `name`-keyed scheme are unaffected
    until backfilled/merged by scripts/merge_duplicate_names.py."""
    statements = [
        "DROP CONSTRAINT person_name IF EXISTS",
        "DROP CONSTRAINT project_name IF EXISTS",
        "DROP CONSTRAINT entity_name IF EXISTS",
        # entity_key existed briefly under the single-generic-Entity-label
        # design this taxonomy replaced — dropped so a database that already
        # picked it up converges onto the per-type constraints below instead
        # of enforcing both. Existing :Entity nodes, if any, are untouched
        # (dropping a constraint doesn't touch data) but stop being written
        # to or queried; see scripts/merge_duplicate_names.py's module
        # docstring for why reclassifying them isn't automated.
        "DROP CONSTRAINT entity_key IF EXISTS",
        "CREATE CONSTRAINT meeting_id IF NOT EXISTS FOR (m:Meeting) REQUIRE m.id IS UNIQUE",
        "CREATE CONSTRAINT person_key IF NOT EXISTS FOR (p:Person) REQUIRE p.key IS UNIQUE",
        "CREATE CONSTRAINT decision_id IF NOT EXISTS FOR (d:Decision) REQUIRE d.id IS UNIQUE",
        "CREATE CONSTRAINT action_item_id IF NOT EXISTS FOR (a:ActionItem) REQUIRE a.id IS UNIQUE",
        "CREATE CONSTRAINT project_key IF NOT EXISTS FOR (pr:Project) REQUIRE pr.key IS UNIQUE",
        "CREATE CONSTRAINT organization_key IF NOT EXISTS FOR (o:Organization) REQUIRE o.key IS UNIQUE",
        "CREATE CONSTRAINT system_key IF NOT EXISTS FOR (s:System) REQUIRE s.key IS UNIQUE",
        "CREATE CONSTRAINT policy_key IF NOT EXISTS FOR (p:Policy) REQUIRE p.key IS UNIQUE",
        "CREATE CONSTRAINT document_key IF NOT EXISTS FOR (d:Document) REQUIRE d.key IS UNIQUE",
        "CREATE CONSTRAINT concept_key IF NOT EXISTS FOR (c:Concept) REQUIRE c.key IS UNIQUE",
    ]
    for stmt in statements:
        run_query(stmt)


def _normalize_key(name: str) -> str:
    """Case/whitespace-insensitive MERGE key for every name-identified
    label (Person, Project, and the knowledge-triple taxonomy — see
    EntityType), so extraction variance across meetings (double spaces,
    "Sarah Park" vs "sarah park", trailing whitespace) lands on the same
    node instead of creating a near-duplicate. Every MERGE on these labels
    uses `key`, then `ON CREATE SET n.name = $name` so the node's display
    `name` is captured once, from whichever meeting introduces it first,
    and never rewritten by a later meeting's differently-cased extraction —
    matching how `display_name` already never changes identity, just what's
    shown."""
    return " ".join(name.strip().split()).lower()


def decision_node_id(meeting_id: str, decision_text: str) -> str:
    return _stable_id(meeting_id, "decision", decision_text)


def build_from_meeting(
    meeting_id: str,
    title: str,
    project: str | None,
    intelligence: MeetingIntelligence,
    date: str | None = None,
) -> None:
    """MERGE this meeting's Meeting/Person/Decision/ActionItem nodes and
    relationships into the graph. Uses deterministic ids so re-processing the
    same meeting updates in place instead of duplicating.

    `date` must be set here — it's what dashboard_service's
    "upcoming_meetings" query orders by, and a property Neo4j has never seen
    on any :Meeting node produces an UnknownPropertyKeyWarning and an
    effectively unordered result rather than an error."""
    run_query(
        "MERGE (m:Meeting {id: $id}) SET m.title = $title, m.date = $date",
        id=meeting_id,
        title=title,
        date=date,
    )

    if project:
        run_query(
            "MERGE (pr:Project {key: $project_key}) ON CREATE SET pr.name = $project "
            "MERGE (m:Meeting {id: $meeting_id}) "
            "MERGE (m)-[:RELATES_TO]->(pr)",
            project=project,
            project_key=_normalize_key(project),
            meeting_id=meeting_id,
        )

    for person in intelligence.participants:
        run_query(
            "MERGE (p:Person {key: $key}) ON CREATE SET p.name = $name "
            "MERGE (m:Meeting {id: $meeting_id}) "
            "MERGE (p)-[:PARTICIPATED_IN]->(m)",
            name=person,
            key=_normalize_key(person),
            meeting_id=meeting_id,
        )

    for decision in intelligence.decisions:
        decision_id = decision_node_id(meeting_id, decision.text)
        run_query(
            "MERGE (d:Decision {id: $id}) "
            "SET d.text = $text, d.confidence = $confidence, d.timestamp = $timestamp, "
            "d.reason = $reason, d.evidence = $evidence "
            "MERGE (m:Meeting {id: $meeting_id}) "
            "MERGE (d)-[:MADE_IN]->(m)",
            id=decision_id,
            text=decision.text,
            confidence=decision.confidence.value,
            timestamp=decision.timestamp,
            reason=decision.reason,
            evidence=decision.evidence,
            meeting_id=meeting_id,
        )
        if decision.speaker.strip():
            run_query(
                "MATCH (d:Decision {id: $id}) "
                "MERGE (p:Person {key: $speaker_key}) ON CREATE SET p.name = $speaker "
                "MERGE (d)-[:MADE_BY]->(p)",
                id=decision_id,
                speaker=decision.speaker,
                speaker_key=_normalize_key(decision.speaker),
            )
        if project:
            run_query(
                "MATCH (d:Decision {id: $id}) "
                "MERGE (pr:Project {key: $project_key}) ON CREATE SET pr.name = $project "
                "MERGE (d)-[:RELATES_TO]->(pr)",
                id=decision_id,
                project=project,
                project_key=_normalize_key(project),
            )

    for item in intelligence.action_items:
        item_id = _stable_id(meeting_id, "action", item.task)
        run_query(
            "MERGE (a:ActionItem {id: $id}) "
            "ON CREATE SET a.completed = false "
            "SET a.task = $task, a.deadline = $deadline, a.priority = $priority "
            "MERGE (m:Meeting {id: $meeting_id}) "
            "MERGE (a)-[:MADE_IN]->(m)",
            id=item_id,
            task=item.task,
            deadline=item.deadline,
            priority=item.priority,
            meeting_id=meeting_id,
        )
        if item.assignee.strip():
            run_query(
                "MATCH (a:ActionItem {id: $id}) "
                "MERGE (p:Person {key: $assignee_key}) ON CREATE SET p.name = $assignee "
                "MERGE (a)-[:ASSIGNED_TO]->(p)",
                id=item_id,
                assignee=item.assignee,
                assignee_key=_normalize_key(item.assignee),
            )

    for triple in intelligence.knowledge_triples:
        # subject_type/object_type are interpolated directly as the Cypher
        # label (Neo4j can't parameterize a label, same restriction as a
        # relationship type). Safe to do here — unlike raw LLM text, they're
        # a Pydantic EntityType enum field, so Pydantic already rejected
        # anything outside that fixed 7-value list before this code ever
        # runs; a Person/Project-typed triple therefore MERGEs onto the same
        # node label + key participants/project already use.
        run_query(
            f"MERGE (s:{triple.subject_type.value} {{key: $subject_key}}) ON CREATE SET s.name = $subject "
            f"MERGE (o:{triple.object_type.value} {{key: $object_key}}) ON CREATE SET o.name = $object "
            "MERGE (s)-[r:RELATES_AS {predicate: $predicate}]->(o) "
            "MERGE (m:Meeting {id: $meeting_id}) "
            "MERGE (s)-[:MENTIONED_IN]->(m) "
            "MERGE (o)-[:MENTIONED_IN]->(m)",
            subject=triple.subject,
            subject_key=_normalize_key(triple.subject),
            object=triple.object,
            object_key=_normalize_key(triple.object),
            predicate=triple.predicate,
            meeting_id=meeting_id,
        )

    logger.info(
        f"[{meeting_id}] Graph updated — {len(intelligence.participants)} people, "
        f"{len(intelligence.decisions)} decisions, {len(intelligence.action_items)} action items, "
        f"{len(intelligence.knowledge_triples)} knowledge triples"
    )


def write_contradiction(from_decision_id: str, to_decision_id: str, message: str) -> None:
    """(Task 4.4) Write a CONTRADICTS edge between two Decision nodes."""
    run_query(
        "MATCH (a:Decision {id: $from_id}), (b:Decision {id: $to_id}) "
        "MERGE (a)-[r:CONTRADICTS]->(b) SET r.message = $message",
        from_id=from_decision_id,
        to_id=to_decision_id,
        message=message,
    )


def delete_meeting(meeting_id: str) -> None:
    """Remove everything this meeting owns from the graph: the Meeting node
    itself plus its Decision/ActionItem nodes (MADE_IN this meeting), and
    every relationship touching them — including CONTRADICTS edges other
    meetings' decisions point at this meeting's decisions, and RELATES_TO
    edges to a Project. DETACH DELETE drops a node's relationships along with
    it, so no separate edge cleanup is needed.

    Person and Project nodes are intentionally left in place even if this
    was their only meeting — they're shared identity nodes (uniqueness
    constraint on name), not owned by any single meeting."""
    run_query(
        "MATCH (m:Meeting {id: $meeting_id}) "
        "OPTIONAL MATCH (d:Decision)-[:MADE_IN]->(m) "
        "OPTIONAL MATCH (a:ActionItem)-[:MADE_IN]->(m) "
        "DETACH DELETE m, d, a",
        meeting_id=meeting_id,
    )


def seed_demo_history() -> str:
    """Idempotently create the synthetic prior meeting(s) that the canned
    demo-mode contradiction flag (gemini_service.demo_meeting_intelligence)
    points at, so DEMO_MODE=true still produces a real, queryable CONTRADICTS
    edge without any live API calls. Returns the seeded decision's node id
    (unchanged — same id the contradiction flag has always pointed at).

    Also seeds one earlier meeting (Task 7.3: a demo dataset that's a real
    multi-meeting story, not one isolated contradiction) connected to the
    Q2 seed via a shared attendee, so the graph shows Q1 -> Q2 -> the new
    meeting rather than just two floating nodes."""
    from app.services.gemini_service import DEMO_SEED_DECISION_TEXT, DEMO_SEED_MEETING_ID

    origin_meeting_id = "demo-seed-meeting-origin"
    origin_decision_id = decision_node_id(
        origin_meeting_id, "Flag vendor concentration risk ahead of Q3 renewal cycle."
    )
    run_query(
        "MERGE (m:Meeting {id: $meeting_id}) SET m.title = 'Q1 Vendor Risk Review (seed)', m.date = '2026-02-10' "
        "MERGE (d:Decision {id: $decision_id}) "
        "SET d.text = $text, d.confidence = 'soft_agreement', d.timestamp = '00:00:00' "
        "MERGE (d)-[:MADE_IN]->(m) "
        "MERGE (p:Person {key: $person_key}) ON CREATE SET p.name = 'Sarah Park' "
        "MERGE (p)-[:PARTICIPATED_IN]->(m) "
        "MERGE (d)-[:MADE_BY]->(p)",
        meeting_id=origin_meeting_id,
        decision_id=origin_decision_id,
        text="Flag vendor concentration risk ahead of Q3 renewal cycle.",
        person_key=_normalize_key("Sarah Park"),
    )

    seed_decision_id = decision_node_id(DEMO_SEED_MEETING_ID, DEMO_SEED_DECISION_TEXT)
    run_query(
        "MERGE (m:Meeting {id: $meeting_id}) SET m.title = 'Q2 All-Hands (seed)', m.date = '2026-05-03' "
        "MERGE (d:Decision {id: $decision_id}) "
        "SET d.text = $text, d.confidence = 'firm_commitment', d.timestamp = '00:00:00' "
        "MERGE (d)-[:MADE_IN]->(m) "
        "MERGE (p:Person {key: $person_key}) ON CREATE SET p.name = 'Sarah Park' "
        "MERGE (p)-[:PARTICIPATED_IN]->(m) "
        "MERGE (d)-[:MADE_BY]->(p)",
        meeting_id=DEMO_SEED_MEETING_ID,
        decision_id=seed_decision_id,
        text=DEMO_SEED_DECISION_TEXT,
        person_key=_normalize_key("Sarah Park"),
    )
    return seed_decision_id


# Which property this module's callers (set_display_name, the /graph API)
# look a node up by — id for the three types whose identity is an opaque
# generated id, name for the rest, whose identity IS their human-readable
# name (Person/Project plus the EntityType taxonomy — Organization/System/
# Policy/Document/Concept — that knowledge_triples subjects/objects use).
# For those name-identified types, the actual MERGE key future meetings
# match against is the derived `key` property (see _normalize_key), not
# `name` itself — but `name` stays what everything outside
# build_from_meeting/seed_demo_history looks up by, since it's set once on
# creation (ON CREATE SET) and therefore stays exactly as unique per label
# as `key` is, just human-readable. display_name is intentionally a
# separate property from both: it only overrides what's rendered, so
# renaming never risks a future meeting failing to merge into the same
# node, and never desyncs a Decision's text from what's embedded in Chroma
# for contradiction matching.
_LABEL_TARGETS = {
    "Meeting": "id",
    "Decision": "id",
    "ActionItem": "id",
    "Person": "name",
    "Project": "name",
    "Organization": "name",
    "System": "name",
    "Policy": "name",
    "Document": "name",
    "Concept": "name",
}


def set_display_name(node_type: str, identifier: str, display_name: str | None) -> None:
    """Set (or, with display_name=None, clear) a node's display-only label
    override. `identifier` is the node's real id (Meeting/Decision/
    ActionItem) or real name (Person/Project) — never the override itself.
    node_type is checked against a fixed allowlist before being interpolated
    into the Cypher label position, so this can't be used to inject an
    arbitrary label/query."""
    key = _LABEL_TARGETS.get(node_type)
    if key is None:
        raise ValueError(f"Unknown node type: {node_type}")
    run_query(
        f"MATCH (n:{node_type} {{{key}: $identifier}}) SET n.display_name = $display_name",
        identifier=identifier,
        display_name=display_name,
    )


def _stable_id(meeting_id: str, kind: str, text: str) -> str:
    """Deterministic id so re-processing the same meeting MERGEs instead of
    duplicating nodes."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"corporate-brain://{meeting_id}/{kind}/{text}"))


def action_item_id(meeting_id: str, task: str) -> str:
    """Public wrapper so callers outside this module (meetings.py's
    /summary endpoint, which never queries Neo4j itself — it just reads the
    stored intelligence JSON) can compute the exact same id
    write_meeting_intelligence used for this task, without duplicating the
    id formula or adding a Neo4j round-trip just to look it up."""
    return _stable_id(meeting_id, "action", task)


def get_action_item(item_id: str) -> dict | None:
    """Task/assignee/completed for one ActionItem — used to permission-check
    (assignee-or-management) a completion update before making it, and to
    404 on an id that doesn't exist in the graph. None if not found."""
    rows = run_query(
        "MATCH (a:ActionItem {id: $id}) "
        "OPTIONAL MATCH (a)-[:ASSIGNED_TO]->(p:Person) "
        "RETURN a.task AS task, p.name AS assignee, coalesce(a.completed, false) AS completed",
        id=item_id,
    )
    return rows[0] if rows else None


def get_action_item_completions(meeting_id: str) -> dict[str, bool]:
    """id -> completed for every ActionItem belonging to this meeting, so
    /meeting/{id}/summary can annotate each entry from the stored
    intelligence JSON (which has no completed field of its own — that only
    ever lives in the graph, set via set_action_item_completed) without one
    round-trip per item. A meeting whose graph write hasn't run yet (still
    processing) or a task added since the last graph write both correctly
    fall through to the caller's own not-completed default."""
    rows = run_query(
        "MATCH (a:ActionItem)-[:MADE_IN]->(:Meeting {id: $meeting_id}) "
        "RETURN a.id AS id, coalesce(a.completed, false) AS completed",
        meeting_id=meeting_id,
    )
    return {row["id"]: row["completed"] for row in rows}


def set_action_item_completed(item_id: str, completed: bool) -> None:
    """The only writer of ActionItem.completed. Deliberately separate from
    write_meeting_intelligence's MERGE above, which never touches this
    field after node creation — so reprocessing the same meeting (e.g. a
    retry) can't silently undo a user's completion."""
    run_query(
        "MATCH (a:ActionItem {id: $id}) SET a.completed = $completed",
        id=item_id,
        completed=completed,
    )
