"""Pre-meeting brief: related previous meetings (same project, earlier
date), the decisions/action items/contradictions/risks drawn from them, and
a suggested agenda. Scoped by the same self-or-management rule as every
other endpoint — related meetings the caller can't otherwise see are
excluded, not just noted."""
import json

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logger import get_logger
from app.graph.neo4j_service import run_query
from app.models.employee import Employee, MeetingParticipant
from app.services.gemini_client import generate_content, has_gemini_credentials
from app.services.storage_service import StorageService

logger = get_logger(__name__)
settings = get_settings()
storage = StorageService()


def _accessible_meeting_ids(caller: Employee, db: Session) -> "set[str] | None":
    if caller.is_management:
        return None
    return {mp.meeting_id for mp in db.query(MeetingParticipant).filter_by(employee_id=caller.id)}


def _related_meetings(meeting_id: str, meeting_ids: "set[str] | None") -> list[dict]:
    """Previous meetings sharing the same project. `other.id <> $id` alone
    would also include meetings scheduled *after* this one, which makes no
    sense for a "previous meetings" brief — the date comparison excludes
    those, falling through to including a meeting when either side's date
    is missing (Meeting.date is optional) rather than dropping it just
    because a date wasn't captured."""
    rows = run_query(
        "MATCH (m:Meeting {id: $id})-[:RELATES_TO]->(pr:Project) "
        "MATCH (other:Meeting)-[:RELATES_TO]->(pr) "
        "WHERE other.id <> $id "
        "AND (m.date IS NULL OR other.date IS NULL OR other.date < m.date) "
        "RETURN other.id AS id, other.title AS title, other.date AS date "
        "ORDER BY other.date DESC",
        id=meeting_id,
    )
    if meeting_ids is None:
        return rows
    return [r for r in rows if r["id"] in meeting_ids]


def _decisions_for(related_ids: list[str]) -> list[dict]:
    if not related_ids:
        return []
    return run_query(
        "MATCH (d:Decision)-[:MADE_IN]->(m:Meeting) WHERE m.id IN $ids "
        "OPTIONAL MATCH (d)-[:MADE_BY]->(p:Person) "
        "RETURN d.text AS text, d.confidence AS confidence, p.name AS speaker, "
        "m.title AS meeting ORDER BY d.timestamp",
        ids=related_ids,
    )


def _action_items_for(related_ids: list[str]) -> list[dict]:
    if not related_ids:
        return []
    return run_query(
        "MATCH (a:ActionItem)-[:MADE_IN]->(m:Meeting) WHERE m.id IN $ids "
        "OPTIONAL MATCH (a)-[:ASSIGNED_TO]->(p:Person) "
        "RETURN a.task AS task, p.name AS assignee, a.deadline AS deadline, "
        "m.title AS meeting ORDER BY a.deadline",
        ids=related_ids,
    )


def _contradictions_for(related_ids: list[str]) -> list[dict]:
    if not related_ids:
        return []
    return run_query(
        "MATCH (d:Decision)-[:MADE_IN]->(m:Meeting) WHERE m.id IN $ids "
        "MATCH (d)-[c:CONTRADICTS]->(other:Decision)-[:MADE_IN]->(otherMeeting:Meeting) "
        "WHERE otherMeeting.id IN $ids "
        "RETURN d.text AS decision, other.text AS conflicts_with, "
        "c.message AS message, m.title AS meeting",
        ids=related_ids,
    )


def _risks_for(related_ids: list[str]) -> list[str]:
    risks: list[str] = []
    for meeting_id in related_ids:
        try:
            data = json.loads(storage.get_file(f"summaries/{meeting_id}.json"))
            risks.extend(data.get("risks") or [])
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    return risks


def _suggested_agenda(decisions: list[dict], action_items: list[dict], contradictions: list[dict]) -> list[str]:
    """Gemini-synthesized from only the retrieved data (same
    don't-invent-facts constraint as askcoco_service), with a deterministic
    fallback if Gemini is unavailable — matching the no-LLM-fallback
    convention used everywhere else in this codebase."""
    if not (decisions or action_items or contradictions):
        return []
    if has_gemini_credentials():
        try:
            prompt = f"""You are drafting a short suggested agenda for an upcoming meeting,
using only the data below from related previous meetings. Do not invent
facts not present in the data. Return 2-4 short bullet points, one per
line, no markdown formatting.

Decisions: {decisions[:10]}
Action items: {action_items[:10]}
Contradictions: {contradictions[:5]}
"""
            response = generate_content(model=settings.gemini_model, contents=prompt)
            text = (response.text or "").strip()
            if text:
                return [line.strip("- ").strip() for line in text.splitlines() if line.strip()]
        except Exception as exc:
            logger.warning("Brief: Gemini agenda synthesis failed, using fallback: %s", exc)

    agenda: list[str] = [f"Follow up on: {item.get('task')}" for item in action_items[:3]]
    agenda += [
        f"Revisit contradiction: {c.get('decision')} vs. {c.get('conflicts_with')}"
        for c in contradictions[:2]
    ]
    return agenda


def generate_brief(meeting_id: str, caller: Employee, db: Session) -> dict:
    meeting_ids_scope = _accessible_meeting_ids(caller, db)
    related = _related_meetings(meeting_id, meeting_ids_scope)
    related_ids = [r["id"] for r in related]

    decisions = _decisions_for(related_ids)
    action_items = _action_items_for(related_ids)
    contradictions = _contradictions_for(related_ids)

    return {
        "meeting_id": meeting_id,
        "related_meetings": related,
        "decisions": decisions,
        # No resolved/unresolved tracking exists anywhere in this data
        # model (frontend action-item status is a hardcoded display
        # default, never round-tripped to the backend) — labeled plainly
        # as "related", never claimed as "open"/"unresolved".
        "related_action_items": action_items,
        "contradictions": contradictions,
        "risks": _risks_for(related_ids),
        "suggested_agenda": _suggested_agenda(decisions, action_items, contradictions),
    }
