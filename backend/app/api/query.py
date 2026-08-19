"""POST /query — predefined Cypher-template Ask Coco queries (Task 5.5)."""
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import get_current_employee
from app.database.session import get_db
from app.models.employee import Employee, MeetingParticipant
from app.services import askcoco_service

router = APIRouter()


def _accessible_meeting_ids(caller: Employee, db: Session) -> "set[str] | None":
    """None means unrestricted (management); otherwise the caller's own
    MeetingParticipant set — same rule as every other locked-down
    endpoint."""
    if caller.is_management:
        return None
    return {mp.meeting_id for mp in db.query(MeetingParticipant).filter_by(employee_id=caller.id)}


class QueryRequest(BaseModel):
    query: str


class Citation(BaseModel):
    filename: str
    timestamp: str
    speaker: str
    excerpt: str


class QueryResponse(BaseModel):
    answer: str
    results: list[dict[str, Any]] = Field(default_factory=list)
    cypher: str = ""
    citations: list[Citation] = Field(default_factory=list)


@router.post("/query", response_model=QueryResponse)
def ask_coco_query(
    payload: QueryRequest,
    db: Session = Depends(get_db),
    caller: Employee = Depends(get_current_employee),
) -> QueryResponse:
    result = askcoco_service.ask(payload.query, _accessible_meeting_ids(caller, db))
    return QueryResponse(**result)


@router.post("/api/chat", response_model=QueryResponse)
def ask_coco_chat(
    payload: QueryRequest,
    db: Session = Depends(get_db),
    caller: Employee = Depends(get_current_employee),
) -> QueryResponse:
    result = askcoco_service.ask(payload.query, _accessible_meeting_ids(caller, db))
    return QueryResponse(**result)
