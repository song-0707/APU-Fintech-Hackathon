# Dashboard & Meeting Intelligence Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the dashboard (blue hero card, invitation accept/reject, task view) and Meeting Intelligence's meeting grid (pagination, source-aware cards), backed by a new `source`/`room_id` tag on `Meeting` and real backend RSVP persistence.

**Architecture:** Backend adds two nullable `Meeting` columns and a new `MeetingInvite` table, extends `POST /meetings`, `GET /meetings`, and `require_meeting_access`, adds `POST /meetings/{id}/rsvp`, and tags the two existing meeting-creation paths (`POST /upload`, `live_meeting.py`'s call-end finalization) with their source. Frontend threads the new fields through `api.ts`/`AppContext.tsx`, fixes a pre-existing fetch-once-on-mount bug that would otherwise break multi-user invitations, and rebuilds the dashboard's hero/upcoming/tasks sections plus Meeting Intelligence's pagination.

**Tech Stack:** FastAPI + SQLAlchemy + SQLite (backend), React + TypeScript + Tailwind (frontend), pytest (backend tests only — see Global Constraints).

**Spec:** [docs/superpowers/specs/2026-08-19-dashboard-meeting-intelligence-redesign-design.md](../specs/2026-08-19-dashboard-meeting-intelligence-redesign-design.md)

## Global Constraints

- Reject = decline for that user only, never a global cancel or a purely local dismiss.
- `Meeting.source` is exactly one of `'scheduled' | 'live' | 'upload'`; `room_id` uniqueness is
  only enforced for `'scheduled'` (see Task 2) — two `'live'` meetings sharing a `room_id` is
  expected, not a bug.
- Participant matching across the frontend/backend boundary is always by **name**
  (case-insensitive), never by id — `participant_names`, not `participant_ids`.
- No migration tool exists (`app/main.py` runs `Base.metadata.create_all(bind=engine)`, which only
  creates *new* tables). Any local dev SQLite file created before Task 1 must be deleted so
  `create_all` rebuilds it with the new columns/table — do this before running backend tests.
- Every commit must use Conventional Commits format (project convention, confirmed against
  `git log`).
- The frontend has **no test runner configured** (`package.json` has only `dev`/`build`/`lint`/
  `preview` scripts; `frontend/**/*.test.{ts,tsx}` matches only third-party `node_modules` files,
  zero project-owned tests). Frontend tasks below verify via `npx tsc --noEmit` (from `frontend/`)
  plus manual interaction in the preview browser — do not introduce Vitest/Jest/RTL to do this;
  that's unrequested infrastructure beyond this plan's approved scope.
- Backend tasks use the existing `pytest` + `conftest.py` fixtures (`db_session`, `client`,
  `management_employee`) — `db_session` and `client` share one in-memory SQLite connection via
  `StaticPool`, so seeding via `db_session` and exercising via `client` in the same test works.

---

## Task 1: `Meeting.source`/`room_id` columns + `MeetingInvite` model

**Files:**
- Modify: `backend/app/models/meeting.py:1-33`
- Test: `backend/tests/test_meeting_invite_model.py` (new)

**Interfaces:**
- Produces: `Meeting.source: str | None`, `Meeting.room_id: str | None`,
  `MeetingInvite(id, meeting_id, employee_id, rsvp_status, created_at, updated_at)` — consumed by
  every later task in this plan.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_meeting_invite_model.py
import pytest
from sqlalchemy.exc import IntegrityError

from app.models.employee import Employee
from app.models.meeting import Meeting, MeetingInvite


def test_meeting_source_and_room_id_round_trip(db_session):
    meeting = Meeting(title="Test", source="scheduled", room_id="CORP-AB12", status="scheduled")
    db_session.add(meeting)
    db_session.commit()
    db_session.refresh(meeting)

    assert meeting.source == "scheduled"
    assert meeting.room_id == "CORP-AB12"


def test_meeting_invite_round_trip(db_session):
    employee = Employee(name="Alice Test", email="alice.test@corpbrain.ai")
    meeting = Meeting(title="Test", source="scheduled", room_id="CORP-AB13", status="scheduled")
    db_session.add_all([employee, meeting])
    db_session.commit()

    invite = MeetingInvite(meeting_id=meeting.id, employee_id=employee.id, rsvp_status="pending")
    db_session.add(invite)
    db_session.commit()
    db_session.refresh(invite)

    assert invite.rsvp_status == "pending"
    assert invite.id is not None


def test_meeting_invite_unique_constraint(db_session):
    employee = Employee(name="Bob Test", email="bob.test@corpbrain.ai")
    meeting = Meeting(title="Test", source="scheduled", room_id="CORP-AB14", status="scheduled")
    db_session.add_all([employee, meeting])
    db_session.commit()

    db_session.add(MeetingInvite(meeting_id=meeting.id, employee_id=employee.id, rsvp_status="pending"))
    db_session.commit()

    db_session.add(MeetingInvite(meeting_id=meeting.id, employee_id=employee.id, rsvp_status="accepted"))
    with pytest.raises(IntegrityError):
        db_session.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_meeting_invite_model.py -v`
Expected: FAIL — `TypeError: 'source' is an invalid keyword argument for Meeting` (columns don't
exist yet), or `ImportError: cannot import name 'MeetingInvite'`.

- [ ] **Step 3: Add the columns and the new model**

In `backend/app/models/meeting.py`, add `UniqueConstraint` to the existing sqlalchemy import
(currently `from sqlalchemy import Column, DateTime, ForeignKey, Integer, String`):

```python
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
```

Inside `class Meeting(Base):`, after the existing `progress = Column(Integer, nullable=False, default=0)` line, add:

```python
    source = Column(String, nullable=True)    # 'scheduled' | 'live' | 'upload'
    room_id = Column(String, nullable=True)   # LiveKit room name; set for 'scheduled' and 'live' only
```

After the `Meeting` class's closing (after its `tasks = relationship(...)` line), add the new model:

```python
class MeetingInvite(Base):
    """Pre-processing access + RSVP for a scheduled meeting invitee. Deliberately separate from
    MeetingParticipant (populated only at processing time from the AI-extracted speaker list) —
    this answers a different question: can this employee see this *unprocessed* meeting, and
    what's their RSVP, for the window between scheduling and processing."""
    __tablename__ = "meeting_invites"

    id = Column(String, primary_key=True, default=_new_id)
    meeting_id = Column(String, ForeignKey("meetings.id"), nullable=False)
    employee_id = Column(String, ForeignKey("employees.id"), nullable=False)
    rsvp_status = Column(String, nullable=False, default="pending")  # 'pending' | 'accepted' | 'declined'
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (UniqueConstraint("meeting_id", "employee_id", name="uq_invite_meeting_employee"),)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_meeting_invite_model.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/meeting.py backend/tests/test_meeting_invite_model.py
git commit -m "feat(meetings): add source/room_id columns and MeetingInvite model"
```

---

## Task 2: `POST /meetings` — real scheduling with invites

**Files:**
- Modify: `backend/app/schemas/meeting.py:1-18`
- Modify: `backend/app/api/meetings.py:1-39`
- Test: `backend/tests/test_create_meeting.py` (new)

**Interfaces:**
- Consumes: `Meeting`, `MeetingInvite` from Task 1.
- Produces: `MeetingCreate.participant_names: list[str]`; `MeetingListItem` gains
  `source: str | None`, `room_id: str | None`, `rsvp_status: str | None` (used by Task 4's
  `GET /meetings` and by frontend Task 7); `POST /meetings` now returns a full `MeetingListItem`
  instead of bare `{meeting_id}` — consumed by frontend Task 8's `addMeeting`/`refreshMeetings`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_create_meeting.py
from app.models.employee import Employee
from app.models.meeting import MeetingInvite


def test_create_meeting_auto_accepts_organizer_and_invites_others(client, db_session):
    organizer = Employee(name="Owner Test", email="owner.test@corpbrain.ai")
    invitee = Employee(name="Invitee Test", email="invitee.test@corpbrain.ai")
    db_session.add_all([organizer, invitee])
    db_session.commit()

    response = client.post(
        "/meetings",
        json={"title": "Sprint Planning", "project": "Core", "participant_names": ["Invitee Test"]},
        headers={"X-User-Name": organizer.name},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "scheduled"
    assert body["status"] == "scheduled"
    assert body["rsvp_status"] == "accepted"
    assert body["room_id"].startswith("CORP-")

    invites = db_session.query(MeetingInvite).filter_by(meeting_id=body["id"]).all()
    by_employee = {inv.employee_id: inv.rsvp_status for inv in invites}
    assert by_employee[organizer.id] == "accepted"
    assert by_employee[invitee.id] == "pending"


def test_create_meeting_rejects_unknown_participant(client, db_session):
    organizer = Employee(name="Owner Two", email="owner.two@corpbrain.ai")
    db_session.add(organizer)
    db_session.commit()

    response = client.post(
        "/meetings",
        json={"title": "Sprint Planning", "participant_names": ["Nobody Here"]},
        headers={"X-User-Name": organizer.name},
    )

    assert response.status_code == 400
    assert "Nobody Here" in response.json()["detail"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_create_meeting.py -v`
Expected: FAIL — `KeyError: 'source'` (endpoint still returns bare `{meeting_id}`), or a 422 on
the request body (`participant_names` not yet an accepted field).

- [ ] **Step 3: Extend the schema and rewrite the endpoint**

In `backend/app/schemas/meeting.py`, add `participant_names` to `MeetingCreate`:

```python
class MeetingCreate(BaseModel):
    title: str
    project: str | None = None
    date: str | None = None
    participant_names: list[str] = []
```

Add three fields to `MeetingListItem` (after `audio_filename: str | None = None`):

```python
    source: str | None = None
    room_id: str | None = None
    rsvp_status: str | None = None
```

In `backend/app/api/meetings.py`, add to the top-level imports: `import secrets` (stdlib, near the
existing `import json`), `from sqlalchemy import func` (near `from sqlalchemy.orm import Session`),
and change `from app.models.meeting import Meeting, ProcessingTask` to
`from app.models.meeting import Meeting, MeetingInvite, ProcessingTask`.

Replace the existing `create_meeting` (lines 32-39) with:

```python
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
    invitee_names = {n.strip().lower() for n in payload.participant_names if n.strip()} - {caller.name.lower()}
    invitees = []
    if invitee_names:
        invitees = db.query(Employee).filter(func.lower(Employee.name).in_(invitee_names)).all()
        missing = invitee_names - {e.name.lower() for e in invitees}
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown participant(s): {sorted(missing)}")

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
```

Note: invitee names are validated **before** any write, so a 400 never leaves a half-created
`Meeting` row behind — this matters because nothing else in this function's error paths rolls
back explicitly.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_create_meeting.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full backend suite to check for regressions**

Run: `cd backend && pytest -v`
Expected: PASS — in particular any existing test that calls `POST /meetings` with the old
`{title, project, date}`-only shape must still pass, since `participant_names` defaults to `[]`.

- [ ] **Step 6: Commit**

```bash
git add backend/app/schemas/meeting.py backend/app/api/meetings.py backend/tests/test_create_meeting.py
git commit -m "feat(meetings): persist real invites on POST /meetings"
```

---

## Task 3: `POST /meetings/{id}/rsvp`

**Files:**
- Modify: `backend/app/schemas/meeting.py`
- Modify: `backend/app/api/meetings.py`
- Test: `backend/tests/test_rsvp.py` (new)

**Interfaces:**
- Consumes: `MeetingInvite` (Task 1).
- Produces: `RsvpRequest{status: 'accepted'|'declined'}`; `POST /meetings/{meeting_id}/rsvp` (204
  on success, 404 if the caller has no invite) — consumed by frontend Task 8's `rsvpToMeeting`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_rsvp.py
from app.models.employee import Employee
from app.models.meeting import Meeting, MeetingInvite


def test_rsvp_updates_callers_own_invite(client, db_session):
    invitee = Employee(name="Rsvp Test", email="rsvp.test@corpbrain.ai")
    meeting = Meeting(title="Standup", source="scheduled", room_id="CORP-RS01", status="scheduled")
    db_session.add_all([invitee, meeting])
    db_session.commit()
    db_session.add(MeetingInvite(meeting_id=meeting.id, employee_id=invitee.id, rsvp_status="pending"))
    db_session.commit()

    response = client.post(
        f"/meetings/{meeting.id}/rsvp",
        json={"status": "declined"},
        headers={"X-User-Name": invitee.name},
    )

    assert response.status_code == 204
    invite = db_session.query(MeetingInvite).filter_by(meeting_id=meeting.id, employee_id=invitee.id).first()
    assert invite.rsvp_status == "declined"


def test_rsvp_without_invitation_returns_404(client, db_session):
    stranger = Employee(name="Stranger Test", email="stranger.test@corpbrain.ai")
    meeting = Meeting(title="Standup", source="scheduled", room_id="CORP-RS02", status="scheduled")
    db_session.add_all([stranger, meeting])
    db_session.commit()

    response = client.post(
        f"/meetings/{meeting.id}/rsvp",
        json={"status": "accepted"},
        headers={"X-User-Name": stranger.name},
    )

    assert response.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_rsvp.py -v`
Expected: FAIL — `404 Not Found` on both (route doesn't exist yet), so the second test's own
assertion of 404 passes for the wrong reason while the first fails. Confirm this by checking the
first test's failure specifically, not just that pytest reports one pass one fail.

- [ ] **Step 3: Add the schema and endpoint**

In `backend/app/schemas/meeting.py`, add `from typing import Literal` to the top imports, then:

```python
class RsvpRequest(BaseModel):
    status: Literal["accepted", "declined"]
```

In `backend/app/api/meetings.py`, add `RsvpRequest` to the existing `from app.schemas.meeting import (...)` block, then add below `create_meeting`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_rsvp.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/app/schemas/meeting.py backend/app/api/meetings.py backend/tests/test_rsvp.py
git commit -m "feat(meetings): add POST /meetings/{id}/rsvp"
```

---

## Task 4: `GET /meetings` — invite-based visibility

**Files:**
- Modify: `backend/app/api/meetings.py:127-185` (the `list_meetings` function)
- Test: `backend/tests/test_list_meetings_invites.py` (new)

**Interfaces:**
- Consumes: `MeetingInvite` (Task 1), `MeetingListItem.source/room_id/rsvp_status` (Task 2).
- Produces: `GET /meetings` now includes the caller's own non-declined-invite meetings even before
  `MeetingParticipant` exists for them, and populates `source`/`room_id`/`rsvp_status` on every
  item — consumed by frontend Task 8's `refreshMeetings`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_list_meetings_invites.py
from app.models.employee import Employee
from app.models.meeting import Meeting, MeetingInvite


def test_pending_invite_makes_unprocessed_meeting_visible_to_invitee_only(client, db_session):
    invitee = Employee(name="List Invitee", email="list.invitee@corpbrain.ai")
    outsider = Employee(name="List Outsider", email="list.outsider@corpbrain.ai")
    meeting = Meeting(title="Roadmap Sync", source="scheduled", room_id="CORP-LM01", status="scheduled")
    db_session.add_all([invitee, outsider, meeting])
    db_session.commit()
    db_session.add(MeetingInvite(meeting_id=meeting.id, employee_id=invitee.id, rsvp_status="pending"))
    db_session.commit()

    invitee_response = client.get("/meetings", headers={"X-User-Name": invitee.name})
    outsider_response = client.get("/meetings", headers={"X-User-Name": outsider.name})

    invitee_ids = {item["id"] for item in invitee_response.json()}
    outsider_ids = {item["id"] for item in outsider_response.json()}
    assert meeting.id in invitee_ids
    assert meeting.id not in outsider_ids

    item = next(item for item in invitee_response.json() if item["id"] == meeting.id)
    assert item["source"] == "scheduled"
    assert item["room_id"] == "CORP-LM01"
    assert item["rsvp_status"] == "pending"


def test_declined_invite_excludes_meeting_from_list(client, db_session):
    invitee = Employee(name="Declined Invitee", email="declined.invitee@corpbrain.ai")
    meeting = Meeting(title="Roadmap Sync 2", source="scheduled", room_id="CORP-LM02", status="scheduled")
    db_session.add_all([invitee, meeting])
    db_session.commit()
    db_session.add(MeetingInvite(meeting_id=meeting.id, employee_id=invitee.id, rsvp_status="declined"))
    db_session.commit()

    response = client.get("/meetings", headers={"X-User-Name": invitee.name})

    assert meeting.id not in {item["id"] for item in response.json()}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_list_meetings_invites.py -v`
Expected: FAIL — the first test fails because the invitee's response is missing the meeting
(current access control is `MeetingParticipant`-only) and `item["source"]` would `KeyError` if it
did appear.

- [ ] **Step 3: Extend `list_meetings`**

In `backend/app/api/meetings.py`, replace the access-control block and the `MeetingListItem(...)`
construction inside `list_meetings` (currently lines ~149-184):

```python
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
            audio_filename=Path(meeting.file_path).name if meeting.file_path else None,
            source=meeting.source,
            room_id=meeting.room_id,
            rsvp_status=invite_status_by_meeting.get(meeting.id),
        ))
    return items
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_list_meetings_invites.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full backend suite**

Run: `cd backend && pytest -v`
Expected: PASS — management-caller visibility (unaffected by this change, since
`accessible_ids` stays `None` for them) and existing `MeetingParticipant`-based tests must be
unchanged.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/meetings.py backend/tests/test_list_meetings_invites.py
git commit -m "feat(meetings): include invited meetings in GET /meetings"
```

---

## Task 5: `require_meeting_access` — extended

**Files:**
- Modify: `backend/app/core/auth.py:1-52`
- Test: `backend/tests/test_require_meeting_access.py` (new)

**Interfaces:**
- Consumes: `MeetingInvite` (Task 1).
- Produces: `require_meeting_access` now also grants access via a non-declined `MeetingInvite`,
  closing the gap where Task 4 makes a meeting listable to an invitee but the old version of this
  function would still 403 them on click-through.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_require_meeting_access.py
import pytest
from fastapi import HTTPException

from app.core.auth import require_meeting_access
from app.models.employee import Employee
from app.models.meeting import Meeting, MeetingInvite


def test_accepted_invite_alone_grants_access(db_session):
    employee = Employee(name="Access Invitee", email="access.invitee@corpbrain.ai")
    meeting = Meeting(title="Test", source="scheduled", room_id="CORP-AC01", status="scheduled")
    db_session.add_all([employee, meeting])
    db_session.commit()
    db_session.add(MeetingInvite(meeting_id=meeting.id, employee_id=employee.id, rsvp_status="accepted"))
    db_session.commit()

    require_meeting_access(db_session, meeting.id, employee)  # must not raise


def test_declined_invite_alone_denies_access(db_session):
    employee = Employee(name="Access Decliner", email="access.decliner@corpbrain.ai")
    meeting = Meeting(title="Test", source="scheduled", room_id="CORP-AC02", status="scheduled")
    db_session.add_all([employee, meeting])
    db_session.commit()
    db_session.add(MeetingInvite(meeting_id=meeting.id, employee_id=employee.id, rsvp_status="declined"))
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        require_meeting_access(db_session, meeting.id, employee)
    assert exc_info.value.status_code == 403


def test_no_invite_and_no_participant_row_denies_access(db_session):
    employee = Employee(name="Access Stranger", email="access.stranger@corpbrain.ai")
    meeting = Meeting(title="Test", source="scheduled", room_id="CORP-AC03", status="scheduled")
    db_session.add_all([employee, meeting])
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        require_meeting_access(db_session, meeting.id, employee)
    assert exc_info.value.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_require_meeting_access.py -v`
Expected: FAIL — `test_accepted_invite_alone_grants_access` raises 403 (function only checks
`MeetingParticipant` today).

- [ ] **Step 3: Extend the function**

In `backend/app/core/auth.py`, add `from app.models.meeting import MeetingInvite` to the imports,
then replace `require_meeting_access`:

```python
def require_meeting_access(db: Session, meeting_id: str, caller: Employee) -> None:
    """Shared per-meeting check for meetings.py's summary/transcript/
    graph-data/export/delete endpoints and graph.py's per-meeting graph
    data — one lookup against the stable meeting_participants table rather
    than each endpoint inferring membership its own way. Also grants access
    via a non-declined MeetingInvite, so an invitee who accepted a
    scheduled meeting doesn't lose visibility if they end up outside the
    AI-extracted participant list once it's processed."""
    if caller.is_management:
        return
    is_participant = (
        db.query(MeetingParticipant)
        .filter_by(meeting_id=meeting_id, employee_id=caller.id)
        .first()
        is not None
    )
    has_invite = (
        db.query(MeetingInvite)
        .filter_by(meeting_id=meeting_id, employee_id=caller.id)
        .filter(MeetingInvite.rsvp_status != "declined")
        .first()
        is not None
    )
    if not (is_participant or has_invite):
        raise HTTPException(status_code=403, detail=f"Not authorized to view meeting {meeting_id}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_require_meeting_access.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full backend suite**

Run: `cd backend && pytest -v`
Expected: PASS — existing `MeetingParticipant`-only access tests unaffected.

- [ ] **Step 6: Commit**

```bash
git add backend/app/core/auth.py backend/tests/test_require_meeting_access.py
git commit -m "fix(meetings): grant meeting access via accepted invite, not just participant row"
```

---

## Task 6: Tag `POST /upload` and live-meeting finalization with `source`

**Files:**
- Modify: `backend/app/api/meetings.py:42-67` (`upload_meeting`)
- Modify: `backend/app/api/live_meeting.py:297-323` (`_create_meeting_from_session`)
- Test: `backend/tests/test_upload_source_tag.py` (new)

**Interfaces:**
- Consumes: `Meeting.source`/`room_id` (Task 1).
- Produces: every `Meeting` row is now tagged at creation with which of the three paths produced
  it — consumed by frontend Task 10 (card title branching).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_upload_source_tag.py
import io

from app.models.meeting import Meeting


def test_upload_tags_meeting_source_as_upload(client, db_session, monkeypatch):
    from app.tasks import meeting_tasks
    monkeypatch.setattr(meeting_tasks.process_meeting_task, "delay", lambda meeting_id: None)

    response = client.post(
        "/upload",
        files={"file": ("recording.mp3", io.BytesIO(b"fake audio bytes"), "audio/mpeg")},
        data={"title": "Uploaded Sync"},
    )

    assert response.status_code == 202
    meeting_id = response.json()["meeting_id"]
    meeting = db_session.query(Meeting).filter_by(id=meeting_id).first()
    assert meeting.source == "upload"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_upload_source_tag.py -v`
Expected: FAIL — `assert None == "upload"`.

- [ ] **Step 3: Tag both creation points**

In `backend/app/api/meetings.py`, change line 54 from
`meeting = Meeting(title=meeting_title, project=project, status="queued")` to:

```python
    meeting = Meeting(title=meeting_title, project=project, status="queued", source="upload")
```

In `backend/app/api/live_meeting.py`, inside `_create_meeting_from_session`, change the
`Meeting(...)` construction (currently `title=title, date=..., duration=duration,
file_path=None, status="pending"`) to also pass `source="live", room_id=room_name`:

```python
        meeting = Meeting(
            title=title,
            date=started_at.strftime("%Y-%m-%d %H:%M"),
            duration=duration,
            file_path=None,
            status="pending",
            source="live",
            room_id=room_name,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_upload_source_tag.py -v`
Expected: PASS

- [ ] **Step 5: Run the full backend suite**

Run: `cd backend && pytest -v`
Expected: PASS — including `test_phase_contracts.py`'s existing live-finalization test
(`test_process_live_meeting_task_reads_segments_and_saves_graph`), which must still pass unchanged
since this only adds two new field values to an existing constructor call.

- [ ] **Step 6: Commit**

```bash
git add backend/app/api/meetings.py backend/app/api/live_meeting.py backend/tests/test_upload_source_tag.py
git commit -m "feat(meetings): tag uploaded and live meetings with their source"
```

**Backend is now complete and independently testable — `pytest` from `backend/` should be fully
green before starting the frontend tasks.**

---

## Task 7: Frontend types + `api.ts` plumbing

**Files:**
- Modify: `frontend/src/types/index.ts` (the `Meeting` interface)
- Modify: `frontend/src/services/api.ts:15-26,319-326,465-504`

**Interfaces:**
- Consumes: `MeetingListItem.source/room_id/rsvp_status` (Task 2/4), `POST /meetings` response
  shape (Task 2), `POST /meetings/{id}/rsvp` (Task 3).
- Produces: `Meeting.source/roomId/rsvpStatus` fields; `api.scheduleMeeting(...)`,
  `api.rsvpToMeeting(...)` — consumed by Task 8 (`AppContext.tsx`); `mergeBackendIntoMeeting` now
  passes `source`/`roomId`/`rsvpStatus` through — consumed by Task 10 (`MeetingCard.tsx`).

- [ ] **Step 1: Add the new `Meeting` fields**

In `frontend/src/types/index.ts`, inside the `Meeting` interface, after `graphData?: GraphData;`:

```ts
  source?: 'scheduled' | 'live' | 'upload';
  roomId?: string;
  rsvpStatus?: 'pending' | 'accepted' | 'declined';
```

- [ ] **Step 2: Extend `BackendMeetingListItem` and `STATUS_MAP`**

In `frontend/src/services/api.ts`, add three fields to `BackendMeetingListItem` (after
`audio_filename: string | null;`):

```ts
  source: string | null;
  room_id: string | null;
  rsvp_status: string | null;
```

Add one entry to `STATUS_MAP` (currently `pending`/`queued`/`processing`/`completed`/`failed`/
`retrying`):

```ts
  scheduled: 'Scheduled',
```

Without this, a freshly-scheduled meeting's raw backend status `"scheduled"` falls through to the
map's `|| 'Pending'` default in `mapBackendStatus`, visually misrepresenting it as already queued
for processing.

- [ ] **Step 3: Pass the new fields through `mergeBackendIntoMeeting`**

In `mergeBackendIntoMeeting`, after the `fileSize: base.fileSize,` line, add:

```ts
    source: (item.source as Meeting['source']) ?? base.source,
    roomId: item.room_id ?? base.roomId,
    rsvpStatus: (item.rsvp_status as Meeting['rsvpStatus']) ?? base.rsvpStatus,
```

- [ ] **Step 4: Add `scheduleMeeting` and `rsvpToMeeting`**

Add near the existing `uploadMeeting`/`deleteMeeting` functions:

```ts
export async function scheduleMeeting(
  title: string,
  project: string | undefined,
  date: string,
  participantNames: string[]
): Promise<BackendMeetingListItem> {
  const res = await fetch(`${API_BASE}/meetings`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...identityHeaders() },
    body: JSON.stringify({ title, project, date, participant_names: participantNames }),
  });
  if (!res.ok) throw new Error(`Schedule meeting failed: ${res.status}`);
  return res.json();
}

export async function rsvpToMeeting(meetingId: string, status: 'accepted' | 'declined'): Promise<void> {
  const res = await fetch(`${API_BASE}/meetings/${encodeURIComponent(meetingId)}/rsvp`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...identityHeaders() },
    body: JSON.stringify({ status }),
  });
  if (!res.ok) throw new Error(`RSVP failed: ${res.status}`);
}
```

- [ ] **Step 5: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors. (This project has no frontend test runner — see Global Constraints —
so type-checking plus the manual verification in later tasks is this plan's frontend
verification method.)

- [ ] **Step 6: Commit**

```bash
git add frontend/src/types/index.ts frontend/src/services/api.ts
git commit -m "feat(meetings): add source/room/rsvp fields and schedule/rsvp API calls"
```

---

## Task 8: `AppContext.tsx` — refetch fix, real scheduling, RSVP action

**Files:**
- Modify: `frontend/src/context/AppContext.tsx:460-520` (`AppContextType` interface)
- Modify: `frontend/src/context/AppContext.tsx:669-724` (meeting-load effect)
- Modify: `frontend/src/context/AppContext.tsx:1075-1128` (`addMeeting`)

**Interfaces:**
- Consumes: `api.scheduleMeeting`, `api.rsvpToMeeting`, `api.listMeetings`,
  `api.mergeBackendIntoMeeting` (Task 7).
- Produces: `refreshMeetings()` (private, re-run on mount / identity change / after schedule /
  after RSVP), `rsvpToMeeting(meetingId, status): Promise<void>`,
  `pendingRoomJoin: {roomName, displayName} | null` + `setPendingRoomJoin` on the exported
  context value — consumed by Task 11 (`InvitationCard`), Task 12 (`MeetingRoomView` auto-join),
  Task 13 (`DashboardView`).

- [ ] **Step 1: Fix the fetch-once-on-mount bug and extract `refreshMeetings`**

Replace the existing meeting-load effect (currently a self-contained async IIFE inside
`useEffect(() => {...}, [])` with an `eslint-disable-next-line react-hooks/exhaustive-deps`
comment) with:

```tsx
  const fetchMeetingsFromBackend = async (): Promise<Meeting[]> => {
    const items = await api.listMeetings();
    if (items.length === 0) return [];

    const results = await Promise.allSettled(
      items.map(async (item) => {
        const [summary, transcript, graphData] = await Promise.all([
          api.getMeetingSummary(item.id),
          api.getMeetingTranscript(item.id),
          api.getGraphData(item.id),
        ]);
        return api.mergeBackendIntoMeeting(
          { id: item.id, title: item.title, project: item.project || 'Unassigned' },
          item, summary, transcript, graphData
        );
      })
    );

    const loaded: Meeting[] = [];
    results.forEach((result, i) => {
      if (result.status === 'fulfilled') {
        loaded.push(result.value);
      } else {
        console.error(`[Corporate Brain] Failed to load meeting ${items[i].id} ("${items[i].title}"):`, result.reason);
      }
    });
    return loaded;
  };

  const refreshMeetings = async () => {
    const loaded = await fetchMeetingsFromBackend();
    if (loaded.length > 0) {
      // Replace every previously-loaded real-backend meeting with this
      // fresh set (not just de-dupe by id) -- otherwise switching demo
      // users via switchDemoUser() would leave the PREVIOUS user's
      // backend-scoped meetings sitting in state alongside the new user's,
      // since real backend ids rarely collide across users. Bundled
      // `mtg-...` demo/mock meetings are untouched -- they aren't
      // user-scoped and aren't something this fetch manages.
      setMeetings((prev) => [...loaded, ...prev.filter((m) => m.id.startsWith('mtg-'))]);
    }
  };

  useEffect(() => {
    let cancelled = false;

    refreshMeetings().catch((e) => {
      if (!cancelled) console.warn('[Corporate Brain] Backend not reachable, staying on demo data:', e);
    });

    return () => {
      cancelled = true;
    };
    // currentUser.name is intentionally the trigger: switching identity
    // (switchDemoUser) must refetch, since /meetings is scoped server-side
    // by the X-User-Name header set in the effect above this one.
  }, [currentUser.name]);
```

This is the fix for the confirmed bug: the old effect had `[]` as its dependency array, so
`switchDemoUser()` never re-fetched `/meetings` for the newly active identity.

- [ ] **Step 2: Rewrite `addMeeting` to call the real backend**

Replace the existing `addMeeting` body (currently constructs a local `mtg-${Date.now()}`
placeholder and pushes it directly into `meetings` state):

```tsx
  const addMeeting = async (data: {
    title: string;
    description: string;
    date: string;
    startTime: string;
    endTime: string;
    department: string;
    participantIds: string[];
  }) => {
    const participantNames = data.participantIds.map(id => {
      const emp = employees.find(e => e.id === id);
      return emp ? emp.name : id;
    });

    try {
      await api.scheduleMeeting(data.title, `${data.department} Sync`, `${data.date} ${data.startTime}`, participantNames);
      await refreshMeetings();
    } catch (e) {
      console.warn('[Corporate Brain] Failed to schedule meeting on the backend:', e);
    }

    const newNotifications: Notification[] = data.participantIds.map((empId, index) => {
      const emp = employees.find(e => e.id === empId);
      const recipientName = emp ? emp.name : 'Participant';
      return {
        id: `notif-invite-${Date.now()}-${index}`,
        title: `Meeting Invitation Sent 📅`,
        message: `${currentUser.name} invited you to "${data.title}" scheduled for ${data.date} at ${data.startTime}.`,
        timestamp: 'Just now',
        read: false,
        category: 'meeting',
        type: 'INVITATION',
        meetingId: '',
        senderName: currentUser.name,
        recipientName: recipientName,
        targetTab: 'meetings'
      };
    });

    setNotifications(prev => [...newNotifications, ...prev]);
    setIsCreateMeetingOpen(false);
  };
```

`meetingId: ''` replaces the old `newMeetingId` reference — the notification no longer has a
locally-minted id to point at (the real one only exists after `refreshMeetings()` resolves), and
nothing currently reads `Notification.meetingId` for `INVITATION`-type notifications to navigate
anywhere, so this is a safe simplification, not a silent behavior loss.

- [ ] **Step 3: Add `rsvpToMeeting` and `pendingRoomJoin`**

Add the new state and action near the other `useState` declarations / action functions:

```tsx
  const [pendingRoomJoin, setPendingRoomJoin] = useState<{ roomName: string; displayName: string } | null>(null);

  const rsvpToMeeting = async (meetingId: string, status: 'accepted' | 'declined') => {
    await api.rsvpToMeeting(meetingId, status);
    await refreshMeetings();
  };
```

- [ ] **Step 4: Update `AppContextType` and the provider's returned value**

In the `AppContextType` interface, change `addMeeting`'s signature to return a `Promise<void>`
and add the two new members:

```tsx
  addMeeting: (meetingData: {
    title: string;
    description: string;
    date: string;
    startTime: string;
    endTime: string;
    department: string;
    participantIds: string[];
  }) => Promise<void>;
  rsvpToMeeting: (meetingId: string, status: 'accepted' | 'declined') => Promise<void>;
  pendingRoomJoin: { roomName: string; displayName: string } | null;
  setPendingRoomJoin: (value: { roomName: string; displayName: string } | null) => void;
```

In the `<AppContext.Provider value={{...}}>` object, add `rsvpToMeeting, pendingRoomJoin,
setPendingRoomJoin,` alongside the existing `addMeeting,`.

- [ ] **Step 5: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors — in particular, confirm `CreateMeetingModal.tsx`'s call site for
`addMeeting` still compiles (it doesn't await the returned promise, which is fine; the modal
already closes itself via `addMeeting`'s own `setIsCreateMeetingOpen(false)`, not by assuming
synchronous completion).

- [ ] **Step 6: Manual verification**

Start the dev server (`preview_start` with the `dev` launch config), open the app, open the
browser console. Use the Settings/profile switcher UI already in the app to switch demo users,
and confirm in the Network tab that a fresh `GET /meetings` request fires with the new
`X-User-Name` header each time you switch. This is the one behavior that was silently broken
before this task and has no automated coverage (no frontend test runner — see Global
Constraints), so confirm it directly rather than trusting the type-check alone.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/context/AppContext.tsx
git commit -m "fix(meetings): refetch on identity change; schedule meetings via the real backend"
```

---

## Task 9: Meeting Intelligence — pagination

**Files:**
- Modify: `frontend/src/components/MeetingIntelligenceOverview.tsx:1,113-119`

**Interfaces:**
- Consumes: existing `filteredCompletedMeetings` (unchanged).
- Produces: nothing consumed by other tasks — self-contained.

- [ ] **Step 1: Add pagination state**

Change the import line from `import React, { useMemo, useState } from 'react';` to
`import React, { useEffect, useMemo, useState } from 'react';`. Inside
`MeetingIntelligenceOverview`, after the existing `const [categoryFilter, setCategoryFilter] =
useState('ALL');`:

```tsx
  const [meetingsPage, setMeetingsPage] = useState(0);
  const MEETINGS_PAGE_SIZE = 6;

  useEffect(() => {
    setMeetingsPage(0);
  }, [categoryFilter]);
```

After the existing `filteredCompletedMeetings` `useMemo`:

```tsx
  const pagedCompletedMeetings = useMemo(
    () => filteredCompletedMeetings.slice(meetingsPage * MEETINGS_PAGE_SIZE, (meetingsPage + 1) * MEETINGS_PAGE_SIZE),
    [filteredCompletedMeetings, meetingsPage]
  );
  const totalMeetingsPages = Math.max(1, Math.ceil(filteredCompletedMeetings.length / MEETINGS_PAGE_SIZE));
```

- [ ] **Step 2: Render paged results with `<`/`>` controls**

Replace the `activeSection === 'meetings'` section's grid line (currently
`{filteredCompletedMeetings.length ? <div className="grid grid-cols-1 gap-5 md:grid-cols-2
xl:grid-cols-3">{filteredCompletedMeetings.map(...)}</div> : <EmptyState .../>}`):

```tsx
        {filteredCompletedMeetings.length ? (
          <>
            <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3">
              {pagedCompletedMeetings.map((meeting) => <MeetingCard key={meeting.id} meeting={meeting} onViewDetails={onSelectMeeting} />)}
            </div>
            <div className="flex items-center justify-center gap-4 pt-2">
              <button
                type="button"
                onClick={() => setMeetingsPage((p) => Math.max(0, p - 1))}
                disabled={meetingsPage === 0}
                className="rounded-xl border border-slate-300 bg-white px-3.5 py-2 text-xs font-bold text-slate-700 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200"
              >
                &lt;
              </button>
              <span className="text-xs font-semibold text-slate-500 dark:text-slate-400">Page {meetingsPage + 1} of {totalMeetingsPages}</span>
              <button
                type="button"
                onClick={() => setMeetingsPage((p) => Math.min(totalMeetingsPages - 1, p + 1))}
                disabled={meetingsPage >= totalMeetingsPages - 1}
                className="rounded-xl border border-slate-300 bg-white px-3.5 py-2 text-xs font-bold text-slate-700 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200"
              >
                &gt;
              </button>
            </div>
          </>
        ) : <EmptyState icon={<Layers className="h-6 w-6" />} title="No indexed meetings" description="Completed meetings in this category will appear here once they are processed." />}
```

- [ ] **Step 3: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 4: Manual verification**

In the preview browser, open Meeting Intelligence. With the bundled demo data (7 meetings, of
which several are `'Completed'`), confirm: if there are ≤6 completed meetings both pagination
buttons render disabled and "Page 1 of 1" shows; if you temporarily lower `MEETINGS_PAGE_SIZE` to
`2` for testing, confirm `>` advances pages, `<` returns, and both disable correctly at the
boundaries — then set it back to `6` before committing.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/MeetingIntelligenceOverview.tsx
git commit -m "feat(meetings): paginate the Meeting Intelligence grid at 6 per page"
```

---

## Task 10: `MeetingCard.tsx` — title branches on source

**Files:**
- Modify: `frontend/src/components/MeetingCard.tsx:62-65`

**Interfaces:**
- Consumes: `Meeting.source`/`roomId` (Task 7).

- [ ] **Step 1: Branch the headline**

Replace:

```tsx
          <h3 className="text-base font-bold text-slate-900 dark:text-white group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors line-clamp-1">
            {meeting.title}
          </h3>
```

with:

```tsx
          <h3 className="text-base font-bold text-slate-900 dark:text-white group-hover:text-blue-600 dark:group-hover:text-blue-400 transition-colors line-clamp-1">
            {meeting.source === 'live' && meeting.roomId ? `Room ${meeting.roomId}` : meeting.title}
          </h3>
```

`MeetingDetailView.tsx`'s own header is untouched by this task — it keeps using `meeting.title`
directly, so the full descriptive backend-generated title is still what shows once you're inside
the meeting.

- [ ] **Step 2: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 3: Manual verification**

In the preview browser, confirm existing completed demo meetings (all `source: undefined` today,
since Task 6 only tags newly-created rows) still show their normal titles — the `undefined ===
'live'` branch is `false`, so nothing regresses for pre-existing data.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/MeetingCard.tsx
git commit -m "feat(meetings): show room ID as the card title for live-sourced meetings"
```

---

## Task 11: New `InvitationCard.tsx`

**Files:**
- Create: `frontend/src/components/InvitationCard.tsx`

**Interfaces:**
- Consumes: `Meeting` type (Task 7).
- Produces: `InvitationCard` component — consumed by Task 13 (`DashboardView`).

- [ ] **Step 1: Create the component**

```tsx
import React from 'react';
import { Meeting } from '../types';

interface InvitationCardProps {
  meeting: Meeting;
  onEnterRoom: (meeting: Meeting) => void;
  onReject: (meetingId: string) => void;
}

export const InvitationCard: React.FC<InvitationCardProps> = ({ meeting, onEnterRoom, onReject }) => {
  return (
    <div className="bg-white dark:bg-slate-900 border border-slate-200/80 dark:border-slate-800 rounded-2xl p-4 shadow-2xs space-y-3">
      <div className="space-y-1">
        <h4 className="text-sm font-bold text-slate-900 dark:text-white">{meeting.title}</h4>
        <p className="text-xs text-slate-500 dark:text-slate-400">{meeting.participants.join(', ')}</p>
        <p className="text-xs text-slate-400">{meeting.dateTime}</p>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <button
          type="button"
          onClick={() => onEnterRoom(meeting)}
          className="py-2.5 px-3 rounded-xl bg-blue-600 hover:bg-blue-700 text-white font-semibold text-xs transition-all cursor-pointer"
        >
          Enter Room
        </button>
        <button
          type="button"
          onClick={() => onReject(meeting.id)}
          className="py-2.5 px-3 rounded-xl bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-700 text-slate-700 dark:text-slate-300 font-semibold text-xs transition-all cursor-pointer"
        >
          Reject
        </button>
      </div>
    </div>
  );
};
```

Both buttons share the same `py-2.5 px-3` sizing inside a `grid grid-cols-2` — equal width by
construction, no icons, per the spec.

- [ ] **Step 2: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors (component isn't imported anywhere yet — Task 13 wires it in).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/InvitationCard.tsx
git commit -m "feat(dashboard): add InvitationCard component"
```

---

## Task 12: `MeetingRoomView.tsx` — auto-join from a scheduled meeting

**Files:**
- Modify: `frontend/src/components/MeetingRoomView.tsx:454-476`

**Interfaces:**
- Consumes: `pendingRoomJoin`/`setPendingRoomJoin` from `useApp()` (Task 8).

- [ ] **Step 1: Extract the join logic and add the auto-join effect**

Replace lines 454-476 (the component's opening state block through `joinRoom`):

```tsx
export const MeetingRoomView: React.FC = () => {
  const { currentUser, pendingRoomJoin, setPendingRoomJoin } = useApp();
  const fullscreenRootRef = useRef<HTMLDivElement>(null);
  const [roomName, setRoomName] = useState('');
  const [displayName, setDisplayName] = useState(currentUser.name);
  const [joinDetails, setJoinDetails] = useState<JoinDetails | null>(null);
  const [error, setError] = useState('');
  const [isJoining, setIsJoining] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);

  useEffect(() => {
    const updateFullscreenState = () => setIsFullscreen(document.fullscreenElement === fullscreenRootRef.current);
    document.addEventListener('fullscreenchange', updateFullscreenState);
    return () => document.removeEventListener('fullscreenchange', updateFullscreenState);
  }, []);

  const joinWithRoomName = async (targetRoomName: string, targetDisplayName: string) => {
    setIsJoining(true); setError('');
    try { setJoinDetails(await getJoinDetails(targetRoomName.trim(), targetDisplayName.trim())); }
    catch (joinError) { setError(joinError instanceof Error ? joinError.message : 'Unable to join meeting.'); }
    finally { setIsJoining(false); }
  };

  const joinRoom = async (event: React.FormEvent) => {
    event.preventDefault();
    await joinWithRoomName(roomName, displayName);
  };

  // Arriving here via InvitationCard's "Enter Room" skips the manual entry
  // screen and joins the meeting's own room directly. Cleared immediately
  // so navigating away and back to Live Meeting later falls through to the
  // normal manual-entry screen, not a stale auto-join.
  useEffect(() => {
    if (!pendingRoomJoin) return;
    const { roomName: targetRoom, displayName: targetDisplayName } = pendingRoomJoin;
    setPendingRoomJoin(null);
    setRoomName(targetRoom);
    void joinWithRoomName(targetRoom, targetDisplayName);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingRoomJoin]);
```

Everything from `const toggleFullscreen = ...` onward is unchanged.

- [ ] **Step 2: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 3: Manual verification**

In the preview browser, navigate to the Live Meeting tab normally first and confirm the manual
entry screen still works exactly as before (regression check) — full end-to-end auto-join
verification happens in Task 13, once `DashboardView`'s "Enter Room" button can actually set
`pendingRoomJoin`.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/MeetingRoomView.tsx
git commit -m "feat(live-meeting): support auto-joining a room from a scheduled invitation"
```

---

## Task 13: `DashboardView.tsx` — hero card, invitations, task swap

**Files:**
- Modify: `frontend/src/components/DashboardView.tsx` (full section rewrite, lines 1-2, 27-34,
  95-246)

**Interfaces:**
- Consumes: `InvitationCard` (Task 11), `rsvpToMeeting`/`setPendingRoomJoin` (Task 8),
  `Meeting.source`/`roomId` (Task 7).

- [ ] **Step 1: Update imports and destructured context values**

Change line 1-2 from:

```tsx
import React, { useState, useMemo } from 'react';
import { useApp } from '../context/AppContext';
```

to:

```tsx
import React, { useState, useMemo, useRef } from 'react';
import { useApp } from '../context/AppContext';
import { InvitationCard } from './InvitationCard';
import { Meeting } from '../types';
```

Change the destructured values (lines 28-34) from:

```tsx
  const {
    meetings,
    currentUser,
    setActiveTab,
    setSelectedMeetingId,
    processAudioForMeeting
  } = useApp();
```

to:

```tsx
  const {
    meetings,
    currentUser,
    setActiveTab,
    setSelectedMeetingId,
    processAudioForMeeting,
    rsvpToMeeting,
    setPendingRoomJoin
  } = useApp();
```

Add `ClipboardList` to the `lucide-react` import list (currently `Clock, CheckCircle2, Search,
Filter, Calendar, ArrowRight, Upload, FileText, X`).

- [ ] **Step 2: Add dashboard-section state and derived data**

After the existing `const [projectFilter, setProjectFilter] = useState('ALL');`:

```tsx
  const [dashboardSection, setDashboardSection] = useState<'upcoming' | 'tasks'>('upcoming');
  const upcomingSectionRef = useRef<HTMLDivElement>(null);
```

After the existing `filteredCompleted` `useMemo`:

```tsx
  const invitationMeetings = useMemo(
    () => meetings.filter(m => m.source === 'scheduled' && m.status === 'Scheduled'),
    [meetings]
  );

  const myTasks = useMemo(() => {
    return meetings.flatMap(m =>
      (m.actionItems || [])
        .filter(item => item.assignee.toLowerCase() === currentUser.name.toLowerCase())
        .map(item => ({ item, meeting: m }))
    );
  }, [meetings, currentUser.name]);

  const handleEnterRoom = async (meeting: Meeting) => {
    if (!meeting.roomId) return;
    await rsvpToMeeting(meeting.id, 'accepted');
    setPendingRoomJoin({ roomName: meeting.roomId, displayName: currentUser.name });
    setActiveTab('live-meeting');
  };

  const handleReject = (meetingId: string) => {
    void rsvpToMeeting(meetingId, 'declined');
  };

  const scrollToUpcoming = () => {
    setDashboardSection('upcoming');
    upcomingSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
```

- [ ] **Step 3: Replace the header with the blue hero card**

Replace the "Clean Simplified Header" block (currently lines 98-127, from `{/* Clean Simplified
Header */}` through its closing `</div>`) with:

```tsx
      {/* Hero Welcome Card */}
      <div className="rounded-3xl bg-gradient-to-br from-blue-600 to-blue-700 dark:from-blue-700 dark:to-blue-900 p-6 sm:p-8 shadow-lg text-white space-y-6">
        <div>
          <h1 className="text-2xl sm:text-3xl font-extrabold tracking-tight font-sans">
            Welcome back, {currentUser.name.split(' ')[0]}
          </h1>
          <p className="text-sm text-blue-100 mt-1">
            Personalized dashboard showing scheduled meetings and indexed decision records for your account.
          </p>
        </div>

        <div className="grid grid-cols-3 gap-4">
          <button
            type="button"
            onClick={scrollToUpcoming}
            className="rounded-2xl bg-white/10 hover:bg-white/20 transition-colors p-4 text-left cursor-pointer"
          >
            <div className="text-3xl sm:text-4xl font-extrabold font-mono">{upcomingMeetings.length}</div>
            <div className="text-xs sm:text-sm font-semibold text-blue-100 mt-1">Upcoming</div>
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('meetings')}
            className="rounded-2xl bg-white/10 hover:bg-white/20 transition-colors p-4 text-left cursor-pointer"
          >
            <div className="text-3xl sm:text-4xl font-extrabold font-mono">{completedMeetings.length}</div>
            <div className="text-xs sm:text-sm font-semibold text-blue-100 mt-1">Completed</div>
          </button>
          <button
            type="button"
            onClick={() => setDashboardSection(prev => prev === 'tasks' ? 'upcoming' : 'tasks')}
            className="rounded-2xl bg-white/10 hover:bg-white/20 transition-colors p-4 text-left cursor-pointer"
          >
            <div className="text-3xl sm:text-4xl font-extrabold font-mono">{myTasks.length}</div>
            <div className="text-xs sm:text-sm font-semibold text-blue-100 mt-1">Tasks</div>
          </button>
        </div>
      </div>
```

- [ ] **Step 4: Replace "Section 1: Upcoming Meetings" with the swappable section**

Replace the entire "Section 1" block (currently lines 160-246, from `{/* Section 1: Upcoming
Meetings */}` through its closing `</div>`) with:

```tsx
      {/* Section 1: Upcoming Meetings OR My Tasks */}
      <div ref={upcomingSectionRef} className="space-y-3 scroll-mt-6">
        {dashboardSection === 'upcoming' ? (
          <>
            <div className="flex items-center justify-between">
              <h3 className="text-base font-extrabold text-slate-900 dark:text-white font-sans flex items-center space-x-2">
                <Clock className="w-4 h-4 text-amber-500" />
                <span>Upcoming Meetings</span>
              </h3>
              <span className="text-xs text-slate-400 font-semibold">{invitationMeetings.length} items</span>
            </div>

            {invitationMeetings.length === 0 ? (
              <div className="p-6 text-center bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl space-y-1">
                <p className="text-xs font-semibold text-slate-500 dark:text-slate-400">
                  No upcoming meetings scheduled for {currentUser.name}.
                </p>
              </div>
            ) : (
              <div className="space-y-3">
                {invitationMeetings.map((mtg) => (
                  <InvitationCard key={mtg.id} meeting={mtg} onEnterRoom={handleEnterRoom} onReject={handleReject} />
                ))}
              </div>
            )}
          </>
        ) : (
          <>
            <div className="flex items-center justify-between">
              <h3 className="text-base font-extrabold text-slate-900 dark:text-white font-sans flex items-center space-x-2">
                <ClipboardList className="w-4 h-4 text-amber-500" />
                <span>My Tasks</span>
              </h3>
              <button
                onClick={() => setDashboardSection('upcoming')}
                className="text-xs font-bold text-blue-600 dark:text-blue-400 hover:underline cursor-pointer"
              >
                Back to Upcoming Meetings
              </button>
            </div>

            {myTasks.length === 0 ? (
              <div className="p-6 text-center bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl space-y-1">
                <p className="text-xs font-semibold text-slate-500 dark:text-slate-400">
                  No tasks assigned to {currentUser.name}.
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                {myTasks.map(({ item, meeting }) => (
                  <div
                    key={item.id}
                    onClick={() => { setSelectedMeetingId(meeting.id); setActiveTab('meetings'); }}
                    className="bg-white dark:bg-slate-900 border border-slate-200/80 dark:border-slate-800 rounded-2xl p-4 shadow-2xs hover:border-blue-300 dark:hover:border-blue-700 transition-all cursor-pointer flex items-center justify-between gap-4"
                  >
                    <div className="min-w-0">
                      <p className="text-sm font-bold text-slate-900 dark:text-white truncate">{item.task}</p>
                      <p className="text-xs text-slate-400 mt-0.5">From: {meeting.title} • Due {item.dueDate}</p>
                    </div>
                    <ArrowRight className="w-3.5 h-3.5 text-blue-600 dark:text-blue-400 shrink-0" />
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
```

"Section 2: Completed Meetings" and the brief modal below it are unchanged.

- [ ] **Step 5: Type-check**

Run: `cd frontend && npx tsc --noEmit`
Expected: no new errors.

- [ ] **Step 6: Manual verification**

In the preview browser: confirm the blue hero card renders with three clickable numbers; click
**Tasks** and confirm the Upcoming section swaps to a task list (or its empty state) and "Back to
Upcoming Meetings" returns it; click **Upcoming** and confirm it scrolls smoothly to that section;
click **Completed** and confirm it navigates to Meeting Intelligence. Then, as a logged-in demo
user with an actual pending invite (schedule one via "New Meeting" as a different demo user first,
switch to the invited user), confirm the invitation card appears with two equal-width, icon-less
buttons, **Reject** removes it from the list, and **Enter Room** navigates to Live Meeting already
connected (no manual room-code entry) — this is the full end-to-end path Tasks 8, 11, 12, and 13
all combine to produce, so exercise it for real rather than each task's piece in isolation.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/DashboardView.tsx
git commit -m "feat(dashboard): blue hero card, invitation cards, and task view"
```

---

## Not included in this plan

**`MeetingDetailView.tsx` visual polish** — the spec scoped this as "light polish, no structural
change" but deferred specifics to implementation planning. At planning time there still isn't a
concrete, user-approved visual delta to implement (no clash was found between the new hero card's
styling and the existing detail-page tabs — both already use `bg-blue-600` as their active-state
color) — writing a task here would mean inventing unrequested changes, which this plan's
constraints (see Global Constraints) rule out just as firmly as leaving a placeholder would. If
specific polish is wanted, get concrete direction on what should change first, then add it as its
own task.

## Self-Review Notes

- **Spec coverage:** every spec section has a task — data model (Task 1), `POST /meetings` +
  invites (Task 2), RSVP (Task 3), `GET /meetings` access (Task 4), `require_meeting_access`
  (Task 5), source tagging (Task 6), frontend plumbing (Task 7), the refetch fix + real scheduling
  (Task 8), pagination (Task 9), card title branching (Task 10), invitation UI (Task 11),
  room auto-join (Task 12), dashboard redesign (Task 13). The one spec item without a task
  (`MeetingDetailView` polish) is explicitly called out above rather than silently dropped.
- **Placeholder scan:** no TBD/TODO/"add appropriate X" remain; every step has real code or a
  concrete, executable verification action.
- **Type consistency, checked across tasks:** `rsvpToMeeting(meetingId: string, status: 'accepted'
  | 'declined')` matches between Task 3 (backend `RsvpRequest.status: Literal[...]`), Task 7
  (`api.rsvpToMeeting`), Task 8 (`AppContext.rsvpToMeeting`), and Task 13 (`handleReject`/
  `handleEnterRoom`). `Meeting.source`/`roomId` naming matches between Task 7 (type + merge),
  Task 10 (`MeetingCard`), and Task 13 (`invitationMeetings` filter, `handleEnterRoom`).
  `pendingRoomJoin: {roomName, displayName}` shape matches between Task 8 (state) and Task 12
  (consumption) and Task 13 (`setPendingRoomJoin` call site).
