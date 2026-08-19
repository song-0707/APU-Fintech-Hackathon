import json
import secrets
from pathlib import Path

from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.auth import get_current_employee, require_meeting_access
from app.core.logger import get_logger
from app.database.session import get_db
from app.graph import graph_builder
from app.models.employee import Employee, MeetingParticipant
from app.models.meeting import Meeting, MeetingInvite, ProcessingTask
from app.schemas.meeting import (
    MeetingCreate,
    MeetingCreateResponse,
    MeetingListItem,
    MeetingStatusResponse,
    RsvpRequest,
)
from app.services import brief_service, embedding_service
from app.services.storage_service import StorageService
from app.tasks.meeting_tasks import process_meeting_task

router = APIRouter()
logger = get_logger(__name__)
storage = StorageService()

ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".mp4"}


def _generate_room_code(db: Session, attempts: int = 5) -> str:
    for _ in range(attempts):
        code = f"CORP-{secrets.token_hex(2).upper()}"
        if not db.query(Meeting).filter_by(room_id=code).first():
            return code
    raise HTTPException(status_code=503, detail="Could not generate a unique room code, please retry")


@router.post("/meetings", response_model=MeetingListItem)
def create_meeting(
    payload: MeetingCreate,
    db: Session = Depends(get_db),
    caller: Employee = Depends(get_current_employee),
) -> MeetingListItem:
    original_by_lower = {n.strip().lower(): n.strip() for n in payload.participant_names if n.strip()}
    invitee_names = set(original_by_lower) - {caller.name.lower()}
    invitees = []
    if invitee_names:
        invitees = db.query(Employee).filter(func.lower(Employee.name).in_(invitee_names)).all()
        missing = invitee_names - {e.name.lower() for e in invitees}
        if missing:
            missing_original = sorted(original_by_lower[name] for name in missing)
            raise HTTPException(status_code=400, detail=f"Unknown participant(s): {missing_original}")

    room_id = _generate_room_code(db)
    meeting = Meeting(
        title=payload.title, project=payload.project, date=payload.date,
        source="scheduled", room_id=room_id, status="scheduled",
    )
    db.add(meeting)
    db.flush()

    db.add(MeetingInvite(meeting_id=meeting.id, employee_id=caller.id, rsvp_status="accepted"))
    for employee in invitees:
        db.add(MeetingInvite(meeting_id=meeting.id, employee_id=employee.id, rsvp_status="pending"))

    db.commit()
    db.refresh(meeting)
    logger.info(f"Meeting {meeting.id} scheduled by {caller.name} with {len(invitees)} invitee(s)")

    return MeetingListItem(
        id=meeting.id, title=meeting.title, project=meeting.project, date=meeting.date,
        status=meeting.status, progress=0, source=meeting.source, room_id=meeting.room_id,
        rsvp_status="accepted",
    )


@router.post("/meetings/{meeting_id}/rsvp", status_code=204)
def set_rsvp(
    meeting_id: str,
    payload: RsvpRequest,
    db: Session = Depends(get_db),
    caller: Employee = Depends(get_current_employee),
) -> Response:
    invite = db.query(MeetingInvite).filter_by(meeting_id=meeting_id, employee_id=caller.id).first()
    if invite is None:
        raise HTTPException(status_code=404, detail="No invitation found for this meeting")
    invite.rsvp_status = payload.status
    db.commit()
    return Response(status_code=204)


@router.post("/upload", response_model=MeetingCreateResponse, status_code=202)
async def upload_meeting(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    project: str | None = Form(None),
    db: Session = Depends(get_db),
) -> MeetingCreateResponse:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {ext or '(none)'}")

    meeting_title = title or Path(file.filename).stem
    meeting = Meeting(title=meeting_title, project=project, status="queued", source="upload")
    db.add(meeting)
    db.commit()
    db.refresh(meeting)

    content = await file.read()
    relative_path = storage.save_raw_file(meeting.id, file.filename, content)
    meeting.file_path = relative_path
    db.commit()

    process_meeting_task.delay(meeting.id)
    logger.info(f"Meeting {meeting.id} uploaded ({len(content)} bytes), queued for processing")

    return MeetingCreateResponse(meeting_id=meeting.id)


@router.get("/task/{meeting_id}/status", response_model=MeetingStatusResponse)
def get_task_status(meeting_id: str, db: Session = Depends(get_db)) -> MeetingStatusResponse:
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if meeting is None:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found")

    latest_task = (
        db.query(ProcessingTask)
        .filter(ProcessingTask.meeting_id == meeting_id)
        .order_by(ProcessingTask.created_at.desc())
        .first()
    )

    return MeetingStatusResponse(
        meeting_id=meeting.id,
        status=meeting.status,
        progress_percentage=meeting.progress,
        error_message=latest_task.error_message if latest_task else None,
    )


# ── Delete meeting (SQL row + graph nodes + embeddings + files) ────────
@router.delete("/meeting/{meeting_id}", status_code=204)
def delete_meeting(
    meeting_id: str,
    db: Session = Depends(get_db),
    caller: Employee = Depends(get_current_employee),
) -> Response:
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if meeting is None:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found")
    require_meeting_access(db, meeting_id, caller)

    # Graph cleanup must succeed before deleting the SQL row. Otherwise the
    # UI can lose the meeting from the list while stale nodes remain visible
    # in Memory Graph with no normal delete path left.
    try:
        graph_builder.delete_meeting(meeting_id)
    except Exception as exc:
        logger.error(f"Meeting {meeting_id}: graph cleanup failed: {exc}")
        raise HTTPException(status_code=503, detail="Graph cleanup failed; meeting was not deleted. Please retry.")

    try:
        embedding_service.delete_meeting(meeting_id)
    except Exception as exc:
        logger.warning(f"Meeting {meeting_id}: embedding cleanup failed: {exc}")

    storage.delete_meeting_files(meeting_id)

    db.delete(meeting)
    db.commit()

    logger.info(f"Meeting {meeting_id} deleted (graph, embeddings, files, DB row)")
    return Response(status_code=204)


# ── Task 5.1 — Meeting list ────────────────────────────────────────────
@router.get("/meetings", response_model=list[MeetingListItem])
def list_meetings(
    keyword: str | None = None,
    project: str | None = None,
    participant: str | None = None,
    date: str | None = None,
    db: Session = Depends(get_db),
    caller: Employee = Depends(get_current_employee),
) -> list[MeetingListItem]:
    query = db.query(Meeting)
    if keyword:
        query = query.filter(Meeting.title.ilike(f"%{keyword}%"))
    if project:
        query = query.filter(Meeting.project == project)
    if date:
        query = query.filter(Meeting.date == date)

    # Access control, always on: an employee only ever sees meetings they're
    # a recorded participant of. `participant` below stays a plain search
    # filter *within* that set — it used to be the only scoping this
    # endpoint had, which is exactly what let a direct API call see
    # everything by just omitting it.
    accessible_ids: set[str] | None = None
    invite_status_by_meeting: dict[str, str] = {}
    if not caller.is_management:
        accessible_ids = {
            mp.meeting_id
            for mp in db.query(MeetingParticipant).filter_by(employee_id=caller.id)
        }
        invite_status_by_meeting = {
            inv.meeting_id: inv.rsvp_status
            for inv in db.query(MeetingInvite).filter_by(employee_id=caller.id)
        }
        accessible_ids |= {
            meeting_id for meeting_id, status in invite_status_by_meeting.items()
            if status != "declined"
        }

    items: list[MeetingListItem] = []
    for meeting in query.order_by(Meeting.created_at.desc()).all():
        if accessible_ids is not None and meeting.id not in accessible_ids:
            continue

        summary = _load_json(f"summaries/{meeting.id}.json")
        participants = summary.get("participants", []) if summary else []

        if participant and participant not in participants:
            continue

        items.append(MeetingListItem(
            id=meeting.id,
            title=meeting.title,
            project=meeting.project,
            date=meeting.date,
            status=meeting.status,
            progress=meeting.progress,
            decisions_count=len(summary.get("decisions", [])) if summary else 0,
            action_items_count=len(summary.get("action_items", [])) if summary else 0,
            flags_count=len(summary.get("flags", [])) if summary else 0,
            # file_path is the storage-relative path (e.g. "raw/<id>/foo.mp4"),
            # set only by the upload endpoint above -- meetings created via
            # POST /meetings (schedule-now-upload-later) or without ever
            # going through a file upload genuinely have none, and that's
            # real: no source recording exists for them, not just "not
            # loaded yet".
            audio_filename=Path(meeting.file_path).name if meeting.file_path else None,
            source=meeting.source,
            room_id=meeting.room_id,
            rsvp_status=invite_status_by_meeting.get(meeting.id),
        ))
    return items


# ── Task 5.2 — Transcript ──────────────────────────────────────────────
@router.get("/meeting/{meeting_id}/transcript")
def get_meeting_transcript(
    meeting_id: str,
    db: Session = Depends(get_db),
    caller: Employee = Depends(get_current_employee),
) -> dict:
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if meeting is None:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found")
    require_meeting_access(db, meeting_id, caller)

    transcript = _load_json(f"transcripts/{meeting_id}.json")
    if transcript is None:
        raise HTTPException(status_code=202, detail=f"Transcript not ready yet: {meeting.status}")
    return transcript


# ── Task 5.3 — Summary (decisions, action items, flags) ────────────────
@router.get("/meeting/{meeting_id}/summary")
def get_meeting_summary(
    meeting_id: str,
    db: Session = Depends(get_db),
    caller: Employee = Depends(get_current_employee),
) -> dict:
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if meeting is None:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found")
    require_meeting_access(db, meeting_id, caller)

    summary = _load_json(f"summaries/{meeting_id}.json")
    if summary is None:
        raise HTTPException(status_code=202, detail=f"Summary not ready yet: {meeting.status}")
    return summary


# ── Pre-Meeting Brief ────────────────────────────────────────────────────
@router.get("/meetings/{meeting_id}/brief")
def get_meeting_brief(
    meeting_id: str,
    db: Session = Depends(get_db),
    caller: Employee = Depends(get_current_employee),
) -> dict:
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if meeting is None:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found")
    require_meeting_access(db, meeting_id, caller)
    return brief_service.generate_brief(meeting_id, caller, db)


# ── Task 7.2 — Export Report ────────────────────────────────────────────
@router.get("/meeting/{meeting_id}/export")
def export_meeting_report(
    meeting_id: str,
    db: Session = Depends(get_db),
    caller: Employee = Depends(get_current_employee),
) -> Response:
    meeting = db.query(Meeting).filter(Meeting.id == meeting_id).first()
    if meeting is None:
        raise HTTPException(status_code=404, detail=f"Meeting {meeting_id} not found")
    require_meeting_access(db, meeting_id, caller)

    summary = _load_json(f"summaries/{meeting_id}.json")
    if summary is None:
        raise HTTPException(status_code=202, detail=f"Summary not ready yet: {meeting.status}")

    report = _build_report_markdown(meeting, summary)
    safe_title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in meeting.title).strip() or meeting.id
    filename = f"{safe_title.replace(' ', '_')}_report.md"

    logger.info(f"Meeting {meeting_id} report exported ({len(report)} bytes)")
    return Response(
        content=report,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _build_report_markdown(meeting: Meeting, summary: dict) -> str:
    lines = [
        f"# {meeting.title}",
        "",
        f"- **Project:** {meeting.project or '—'}",
        f"- **Date:** {meeting.date or '—'}",
        f"- **Duration:** {summary.get('duration', '—')}",
        f"- **Participants:** {', '.join(summary.get('participants', [])) or '—'}",
        f"- **Exported:** {datetime.utcnow().isoformat(timespec='seconds')}Z",
        "",
        "## Summary",
        "",
        summary.get("summary") or "_No summary available._",
        "",
    ]

    flags = summary.get("flags") or []
    if flags:
        lines += ["## AI Flags", ""]
        for f in flags:
            lines.append(f"- **[{f.get('severity', 'warning').upper()}] {f.get('type', 'flag')}:** {f.get('message', '')}")
        lines.append("")

    decisions = summary.get("decisions") or []
    lines += ["## Decisions", ""]
    if decisions:
        for d in decisions:
            title = d.get("title") or d.get("text") or "Untitled decision"
            lines.append(f"### {title}")
            lines.append(f"- **Confidence:** {d.get('confidence', '—')}")
            if d.get("reason"):
                lines.append(f"- **Reason:** {d['reason']}")
            if d.get("evidence"):
                lines.append(f"- **Evidence:** {d['evidence']}")
            lines.append(f"- **When:** {d.get('timestamp', '—')} — {d.get('speaker', '—')}")
            lines.append("")
    else:
        lines += ["_No decisions recorded._", ""]

    action_items = summary.get("action_items") or []
    lines += ["## Action Items", ""]
    if action_items:
        for a in action_items:
            deadline = f" (due {a['deadline']})" if a.get("deadline") else ""
            lines.append(f"- [{a.get('priority', 'medium')}] {a.get('task', '')} — **{a.get('assignee', 'Unassigned')}**{deadline}")
        lines.append("")
    else:
        lines += ["_No action items recorded._", ""]

    risks = summary.get("risks") or []
    if risks:
        lines += ["## Risks", ""]
        lines += [f"- {r}" for r in risks]
        lines.append("")

    return "\n".join(lines)


def _load_json(relative_path: str) -> dict | None:
    """Best-effort read of a StorageService-saved JSON file. None if the
    meeting hasn't reached that pipeline stage yet."""
    try:
        return json.loads(storage.get_file(relative_path))
    except FileNotFoundError:
        return None
