"""GET /meeting/{id}/graph-data (Task 5.4) — {nodes, links} for
react-force-graph, sourced from Neo4j. CONTRADICTS links get
isContradiction: true so the frontend can render them distinctly (Task 6.6).

PATCH /graph/node-label sets a node's display-only label override (Task —
memory graph customization): every query below returns each name-identified
node's (Person/Project, plus the EntityType taxonomy — Organization/System/
Policy/Document/Concept — graph_builder gives knowledge_triples subjects/
objects) real `name` (used to build the node id, so identity/merging never
changes) separately from `coalesce(display_name, name)` (used only for the
rendered label). Meeting/Decision/ActionItem already have a separate id
field from their title/text/task, so those are coalesced in place.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.auth import get_current_employee, require_access, require_meeting_access
from app.core.logger import get_logger
from app.database.session import get_db
from app.graph import graph_builder, neo4j_service
from app.models.employee import Employee, MeetingParticipant

router = APIRouter()
logger = get_logger(__name__)


class SetNodeLabelRequest(BaseModel):
    node_type: str
    identifier: str
    display_name: str | None = None


@router.patch("/graph/node-label")
def set_node_label(payload: SetNodeLabelRequest) -> dict:
    """display_name=None (or omitted) clears the override, reverting to the
    node's real name/title/text/task."""
    try:
        graph_builder.set_display_name(payload.node_type, payload.identifier, payload.display_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True}


def _drop_dangling_links(nodes: dict[str, dict], links: list[dict]) -> list[dict]:
    """Safety net for react-force-graph-2d's d3-force layer, which throws
    "node not found" if a link's source/target id has no matching node.
    Each block above issues its own separate Neo4j query/transaction, so a
    node written concurrently (e.g. by a background meeting-processing task)
    between two of those queries can otherwise be referenced by a link
    without ever being added to `nodes` — drop such links rather than
    shipping a payload the frontend can't render."""
    valid_ids = nodes.keys()
    kept = [l for l in links if l["source"] in valid_ids and l["target"] in valid_ids]
    if len(kept) != len(links):
        logger.warning(
            f"Dropped {len(links) - len(kept)} graph link(s) referencing a node "
            f"missing from this response's nodes[]"
        )
    return kept


@router.get("/meeting/{meeting_id}/graph-data")
def get_meeting_graph_data(
    meeting_id: str,
    db: Session = Depends(get_db),
    caller: Employee = Depends(get_current_employee),
) -> dict:
    require_meeting_access(db, meeting_id, caller)

    meeting_rows = neo4j_service.run_query(
        "MATCH (m:Meeting {id: $id}) RETURN m.id AS id, coalesce(m.display_name, m.title) AS title",
        id=meeting_id,
    )
    if not meeting_rows:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found in graph")

    nodes: dict[str, dict] = {}
    links: list[dict] = []
    meeting_node_id = f"meeting:{meeting_id}"
    nodes[meeting_node_id] = {
        "id": meeting_node_id,
        "label": meeting_rows[0]["title"] or meeting_id,
        "type": "Meeting",
    }

    for row in neo4j_service.run_query(
        "MATCH (p:Person)-[:PARTICIPATED_IN]->(m:Meeting {id: $id}) "
        "RETURN p.name AS name, coalesce(p.display_name, p.name) AS label",
        id=meeting_id,
    ):
        pid = f"person:{row['name']}"
        nodes[pid] = {"id": pid, "label": row["label"], "type": "Person"}
        links.append({"source": pid, "target": meeting_node_id, "type": "PARTICIPATED_IN"})

    for row in neo4j_service.run_query(
        """MATCH (d:Decision)-[:MADE_IN]->(m:Meeting {id: $id})
           OPTIONAL MATCH (d)-[:MADE_BY]->(p:Person)
           RETURN d.id AS id, coalesce(d.display_name, d.text) AS text,
                  p.name AS speaker, coalesce(p.display_name, p.name) AS speaker_label""",
        id=meeting_id,
    ):
        did = f"decision:{row['id']}"
        nodes[did] = {"id": did, "label": row["text"], "type": "Decision"}
        links.append({"source": did, "target": meeting_node_id, "type": "MADE_IN"})
        if row["speaker"]:
            pid = f"person:{row['speaker']}"
            nodes.setdefault(pid, {"id": pid, "label": row["speaker_label"], "type": "Person"})
            links.append({"source": did, "target": pid, "type": "MADE_BY"})

    for row in neo4j_service.run_query(
        """MATCH (a:ActionItem)-[:MADE_IN]->(m:Meeting {id: $id})
           OPTIONAL MATCH (a)-[:ASSIGNED_TO]->(p:Person)
           RETURN a.id AS id, coalesce(a.display_name, a.task) AS task,
                  p.name AS assignee, coalesce(p.display_name, p.name) AS assignee_label""",
        id=meeting_id,
    ):
        aid = f"action:{row['id']}"
        nodes[aid] = {"id": aid, "label": row["task"], "type": "ActionItem"}
        links.append({"source": aid, "target": meeting_node_id, "type": "MADE_IN"})
        if row["assignee"]:
            pid = f"person:{row['assignee']}"
            nodes.setdefault(pid, {"id": pid, "label": row["assignee_label"], "type": "Person"})
            links.append({"source": aid, "target": pid, "type": "ASSIGNED_TO"})

    for row in neo4j_service.run_query(
        "MATCH (m:Meeting {id: $id})-[:RELATES_TO]->(pr:Project) "
        "RETURN pr.name AS name, coalesce(pr.display_name, pr.name) AS label",
        id=meeting_id,
    ):
        prid = f"project:{row['name']}"
        nodes[prid] = {"id": prid, "label": row["label"], "type": "Project"}
        links.append({"source": meeting_node_id, "target": prid, "type": "RELATES_TO"})

    for row in neo4j_service.run_query(
        """MATCH (d:Decision)-[:MADE_IN]->(:Meeting {id: $id})
           MATCH (d)-[:RELATES_TO]->(pr:Project)
           RETURN d.id AS decision_id, coalesce(d.display_name, d.text) AS decision_text,
                  pr.name AS name, coalesce(pr.display_name, pr.name) AS label""",
        id=meeting_id,
    ):
        did = f"decision:{row['decision_id']}"
        prid = f"project:{row['name']}"
        # Same rationale as the CONTRADICTS block below: don't rely on the
        # decisions loop above having already run for this same decision.
        nodes.setdefault(did, {"id": did, "label": row["decision_text"], "type": "Decision"})
        nodes.setdefault(prid, {"id": prid, "label": row["label"], "type": "Project"})
        links.append({"source": did, "target": prid, "type": "RELATES_TO"})

    # Knowledge-triple subjects/objects mentioned in this meeting — any of
    # the EntityType taxonomy labels (Organization/System/Policy/Document/
    # Concept, or Person/Project if a triple resolved to one of those). `e`
    # is left label-unbound in the MATCH since the whole point is picking up
    # whichever of the fixed labels this node actually has; labels(e)[0] is
    # unambiguous because build_from_meeting never gives a node more than
    # one label.
    for row in neo4j_service.run_query(
        "MATCH (e)-[:MENTIONED_IN]->(m:Meeting {id: $id}) "
        "RETURN e.name AS name, coalesce(e.display_name, e.name) AS label, labels(e)[0] AS node_type",
        id=meeting_id,
    ):
        eid = f"{row['node_type'].lower()}:{row['name']}"
        nodes.setdefault(eid, {"id": eid, "label": row["label"], "type": row["node_type"]})
        links.append({"source": eid, "target": meeting_node_id, "type": "MENTIONED_IN"})

    for row in neo4j_service.run_query(
        """MATCH (s)-[:MENTIONED_IN]->(:Meeting {id: $id})
           MATCH (s)-[r:RELATES_AS]->(o)
           RETURN s.name AS subject, coalesce(s.display_name, s.name) AS subject_label, labels(s)[0] AS subject_type,
                  o.name AS object, coalesce(o.display_name, o.name) AS object_label, labels(o)[0] AS object_type,
                  r.predicate AS predicate""",
        id=meeting_id,
    ):
        sid = f"{row['subject_type'].lower()}:{row['subject']}"
        oid = f"{row['object_type'].lower()}:{row['object']}"
        # Same rationale as CONTRADICTS below: `o` may not be mentioned in
        # *this* meeting (a subject's relationship can point at an object
        # introduced in an earlier meeting), so don't rely on the loop above
        # having already added it.
        nodes.setdefault(sid, {"id": sid, "label": row["subject_label"], "type": row["subject_type"]})
        nodes.setdefault(oid, {"id": oid, "label": row["object_label"], "type": row["object_type"]})
        # predicate (e.g. "USES_VENDOR"), not the literal RELATES_AS
        # relationship type, is the link's `type` — RELATION_PHRASING/its
        # fallback humanizer in KnowledgeGraphView.tsx turns arbitrary
        # SCREAMING_CASE predicate text into a readable edge label, and
        # "relates as" for every single triple would lose exactly the
        # information a knowledge triple exists to carry.
        links.append({"source": sid, "target": oid, "type": row["predicate"]})

    for row in neo4j_service.run_query(
        """MATCH (d:Decision)-[:MADE_IN]->(:Meeting {id: $id})
           MATCH (d)-[c:CONTRADICTS]->(other:Decision)
           RETURN d.id AS from_id, other.id AS to_id,
                  coalesce(other.display_name, other.text) AS to_text, c.message AS message""",
        id=meeting_id,
    ):
        from_did = f"decision:{row['from_id']}"
        to_did = f"decision:{row['to_id']}"
        # `other` is the decision this one contradicts — by design that's
        # often a decision made in a *different* meeting, so it won't
        # already be in `nodes` from the meeting-scoped loop above.
        nodes.setdefault(to_did, {"id": to_did, "label": row["to_text"], "type": "Decision"})
        links.append({
            "source": from_did,
            "target": to_did,
            "type": "CONTRADICTS",
            "isContradiction": True,
            "message": row["message"],
        })

    return {"nodes": list(nodes.values()), "links": _drop_dangling_links(nodes, links)}


@router.get("/graph/contradictions")
def get_contradictions(
    person: str | None = None,
    caller: Employee = Depends(get_current_employee),
) -> list[dict]:
    """Every CONTRADICTS edge in the graph, with enough context (both
    decisions' text, both meetings' id+title, who made the flagged decision)
    for a frontend drill-down — e.g. DashboardView's "Your Flags" count,
    which today shows a number with nothing behind it. `person` defaults to
    the caller and is otherwise resolved/checked by _resolve_target: only
    contradictions in decisions that person made, org-wide only for a
    management caller who omits it."""
    target = _resolve_target(person, caller)
    return neo4j_service.run_query(
        "MATCH (d:Decision)-[c:CONTRADICTS]->(other:Decision) "
        "OPTIONAL MATCH (d)-[:MADE_BY]->(p:Person) "
        # WHERE right after OPTIONAL MATCH is part of that match's pattern,
        # not a row filter — a non-matching condition nulls out p instead of
        # dropping the row (Neo4j's best-known OPTIONAL MATCH gotcha). WITH
        # closes the optional match first so WHERE filters the real row set.
        "WITH d, c, other, p "
        "WHERE $person IS NULL OR toLower(p.name) = toLower($person) "
        "OPTIONAL MATCH (d)-[:MADE_IN]->(m:Meeting) "
        "OPTIONAL MATCH (other)-[:MADE_IN]->(om:Meeting) "
        "RETURN d.id AS decision_id, coalesce(d.display_name, d.text) AS decision_text, "
        "p.name AS speaker, "
        "m.id AS meeting_id, coalesce(m.display_name, m.title) AS meeting_title, "
        "other.id AS contradicts_decision_id, "
        "coalesce(other.display_name, other.text) AS contradicts_decision_text, "
        "om.id AS contradicts_meeting_id, "
        "coalesce(om.display_name, om.title) AS contradicts_meeting_title, "
        "c.message AS message "
        "ORDER BY d.timestamp DESC",
        person=target,
    )


_CONTAINMENT_LINK_TYPES = {
    "PARTICIPATED_IN", "MADE_IN", "MADE_BY", "ASSIGNED_TO", "RELATES_TO", "MENTIONED_IN",
}


def _resolve_target(person: str | None, caller: Employee) -> str | None:
    """Resolve /graph's and /graph/contradictions' `person` param into
    whose meetings to scope to. None means "org-wide" — only reachable by a
    management caller who passed no `person`. Any explicit `person` (self,
    or anyone else if the caller is management) is checked via
    require_access before being returned."""
    if person is not None:
        require_access(person, caller)
        return person
    return None if caller.is_management else caller.name


def _scope_to_meetings(nodes: dict[str, dict], links: list[dict], meeting_ids: set[str]) -> tuple[dict, list]:
    """Restrict a full graph to the given meetings and everything IN them,
    plus one hop out on CONTRADICTS/knowledge-triple edges so the *other*
    side of a flagged contradiction (or a related entity) stays visible —
    hiding why something was flagged would defeat the point of showing it.
    `meeting_ids` comes from the SQL meeting_participants table (the stable
    access-control source — see app/core/auth.py), not from matching a name
    against the graph itself."""
    core_meetings = {f"meeting:{mid}" for mid in meeting_ids}
    if not core_meetings:
        return {}, []

    # Containment edges expand to a fixed point: e.g. a Decision only enters
    # scope via MADE_IN, and its own RELATES_TO->Project edge can only be
    # followed once the Decision itself already is.
    in_scope = set(core_meetings)
    changed = True
    while changed:
        changed = False
        for l in links:
            if l["type"] not in _CONTAINMENT_LINK_TYPES:
                continue
            if l["source"] in in_scope and l["target"] not in in_scope:
                in_scope.add(l["target"])
                changed = True
            elif l["target"] in in_scope and l["source"] not in in_scope:
                in_scope.add(l["source"])
                changed = True

    # One hop of context: CONTRADICTS edges, and knowledge-triple edges
    # (their `type` is the raw predicate — see the RELATES_AS block below —
    # so they never match a name in _CONTAINMENT_LINK_TYPES).
    extra: set[str] = set()
    for l in links:
        is_context_edge = l["type"] == "CONTRADICTS" or l["type"] not in _CONTAINMENT_LINK_TYPES
        if is_context_edge and (l["source"] in in_scope or l["target"] in in_scope):
            extra.add(l["source"])
            extra.add(l["target"])
    in_scope |= extra

    scoped_nodes = {nid: n for nid, n in nodes.items() if nid in in_scope}
    scoped_links = [l for l in links if l["source"] in in_scope and l["target"] in in_scope]
    return scoped_nodes, scoped_links


@router.get("/graph")
def get_global_graph_data(
    person: str | None = None,
    db: Session = Depends(get_db),
    caller: Employee = Depends(get_current_employee),
) -> dict:
    """Whole-organization knowledge graph (Task 6.6's 'Memory Graph' page).
    Defaults to the caller's own meetings (see _scope_to_meetings) — an
    unrecognized/unauthorized caller gets an empty graph, never the full
    org. Pass `person` to view a specific employee's slice instead
    (management-only, checked via _resolve_target); a management caller
    passing no `person` gets the unfiltered org-wide view, unchanged from
    before."""
    nodes: dict[str, dict] = {}
    links: list[dict] = []

    for row in neo4j_service.run_query(
        "MATCH (m:Meeting) RETURN m.id AS id, coalesce(m.display_name, m.title) AS title"
    ):
        mid = f"meeting:{row['id']}"
        nodes[mid] = {"id": mid, "label": row["title"] or row["id"], "type": "Meeting"}

    for row in neo4j_service.run_query(
        "MATCH (p:Person)-[:PARTICIPATED_IN]->(m:Meeting) "
        "RETURN p.name AS name, coalesce(p.display_name, p.name) AS label, m.id AS meeting_id"
    ):
        pid = f"person:{row['name']}"
        nodes.setdefault(pid, {"id": pid, "label": row["label"], "type": "Person"})
        links.append({"source": pid, "target": f"meeting:{row['meeting_id']}", "type": "PARTICIPATED_IN"})

    for row in neo4j_service.run_query(
        """MATCH (d:Decision)-[:MADE_IN]->(m:Meeting)
           OPTIONAL MATCH (d)-[:MADE_BY]->(p:Person)
           RETURN d.id AS id, coalesce(d.display_name, d.text) AS text, m.id AS meeting_id,
                  p.name AS speaker, coalesce(p.display_name, p.name) AS speaker_label"""
    ):
        did = f"decision:{row['id']}"
        nodes[did] = {"id": did, "label": row["text"], "type": "Decision"}
        links.append({"source": did, "target": f"meeting:{row['meeting_id']}", "type": "MADE_IN"})
        if row["speaker"]:
            pid = f"person:{row['speaker']}"
            nodes.setdefault(pid, {"id": pid, "label": row["speaker_label"], "type": "Person"})
            links.append({"source": did, "target": pid, "type": "MADE_BY"})

    for row in neo4j_service.run_query(
        """MATCH (a:ActionItem)-[:MADE_IN]->(m:Meeting)
           OPTIONAL MATCH (a)-[:ASSIGNED_TO]->(p:Person)
           RETURN a.id AS id, coalesce(a.display_name, a.task) AS task, m.id AS meeting_id,
                  p.name AS assignee, coalesce(p.display_name, p.name) AS assignee_label"""
    ):
        aid = f"action:{row['id']}"
        nodes[aid] = {"id": aid, "label": row["task"], "type": "ActionItem"}
        links.append({"source": aid, "target": f"meeting:{row['meeting_id']}", "type": "MADE_IN"})
        if row["assignee"]:
            pid = f"person:{row['assignee']}"
            nodes.setdefault(pid, {"id": pid, "label": row["assignee_label"], "type": "Person"})
            links.append({"source": aid, "target": pid, "type": "ASSIGNED_TO"})

    for row in neo4j_service.run_query(
        "MATCH (m:Meeting)-[:RELATES_TO]->(pr:Project) "
        "RETURN m.id AS meeting_id, pr.name AS name, coalesce(pr.display_name, pr.name) AS label"
    ):
        prid = f"project:{row['name']}"
        nodes.setdefault(prid, {"id": prid, "label": row["label"], "type": "Project"})
        links.append({"source": f"meeting:{row['meeting_id']}", "target": prid, "type": "RELATES_TO"})

    for row in neo4j_service.run_query(
        "MATCH (d:Decision)-[:RELATES_TO]->(pr:Project) "
        "RETURN d.id AS decision_id, coalesce(d.display_name, d.text) AS decision_text, "
        "pr.name AS name, coalesce(pr.display_name, pr.name) AS label"
    ):
        did = f"decision:{row['decision_id']}"
        prid = f"project:{row['name']}"
        # Same rationale as the CONTRADICTS block below: this decision may
        # not have been present in the earlier, separately-queried decisions
        # loop if it was written concurrently with this request.
        nodes.setdefault(did, {"id": did, "label": row["decision_text"], "type": "Decision"})
        nodes.setdefault(prid, {"id": prid, "label": row["label"], "type": "Project"})
        links.append({"source": did, "target": prid, "type": "RELATES_TO"})

    # Knowledge-triple subjects/objects, org-wide — see the matching block
    # in get_meeting_graph_data for why `e`/labels(e)[0] are used instead of
    # querying each EntityType taxonomy label separately.
    for row in neo4j_service.run_query(
        "MATCH (e)-[:MENTIONED_IN]->(m:Meeting) "
        "RETURN e.name AS name, coalesce(e.display_name, e.name) AS label, labels(e)[0] AS node_type, m.id AS meeting_id"
    ):
        eid = f"{row['node_type'].lower()}:{row['name']}"
        nodes.setdefault(eid, {"id": eid, "label": row["label"], "type": row["node_type"]})
        links.append({"source": eid, "target": f"meeting:{row['meeting_id']}", "type": "MENTIONED_IN"})

    for row in neo4j_service.run_query(
        """MATCH (s)-[r:RELATES_AS]->(o)
           WHERE EXISTS { MATCH (s)-[:MENTIONED_IN]->(:Meeting) }
             AND EXISTS { MATCH (o)-[:MENTIONED_IN]->(:Meeting) }
           RETURN s.name AS subject, coalesce(s.display_name, s.name) AS subject_label, labels(s)[0] AS subject_type,
                  o.name AS object, coalesce(o.display_name, o.name) AS object_label, labels(o)[0] AS object_type,
                  r.predicate AS predicate"""
    ):
        sid = f"{row['subject_type'].lower()}:{row['subject']}"
        oid = f"{row['object_type'].lower()}:{row['object']}"
        nodes.setdefault(sid, {"id": sid, "label": row["subject_label"], "type": row["subject_type"]})
        nodes.setdefault(oid, {"id": oid, "label": row["object_label"], "type": row["object_type"]})
        links.append({"source": sid, "target": oid, "type": row["predicate"]})

    for row in neo4j_service.run_query(
        """MATCH (d:Decision)-[c:CONTRADICTS]->(other:Decision)
           RETURN d.id AS from_id, coalesce(d.display_name, d.text) AS from_text,
                  other.id AS to_id, coalesce(other.display_name, other.text) AS to_text,
                  c.message AS message"""
    ):
        from_did = f"decision:{row['from_id']}"
        to_did = f"decision:{row['to_id']}"
        # Both ends are populated directly from this query rather than
        # relying on the decisions loop above: that loop and this one are
        # separate Neo4j transactions (see neo4j_service.run_query), so a
        # decision written concurrently between the two — e.g. by a
        # background meeting-processing task — would otherwise show up in a
        # CONTRADICTS link without ever being added to `nodes`.
        nodes.setdefault(from_did, {"id": from_did, "label": row["from_text"], "type": "Decision"})
        nodes.setdefault(to_did, {"id": to_did, "label": row["to_text"], "type": "Decision"})
        links.append({
            "source": from_did,
            "target": to_did,
            "type": "CONTRADICTS",
            "isContradiction": True,
            "message": row["message"],
        })

    target = _resolve_target(person, caller)
    if target is not None:
        target_employee = (
            caller if target.lower() == caller.name.lower()
            else db.query(Employee).filter(func.lower(Employee.name) == target.lower()).first()
        )
        meeting_ids: set[str] = set()
        if target_employee is not None:
            meeting_ids = {
                mp.meeting_id
                for mp in db.query(MeetingParticipant).filter_by(employee_id=target_employee.id)
            }
        nodes, links = _scope_to_meetings(nodes, links, meeting_ids)

    return {"nodes": list(nodes.values()), "links": _drop_dangling_links(nodes, links)}
