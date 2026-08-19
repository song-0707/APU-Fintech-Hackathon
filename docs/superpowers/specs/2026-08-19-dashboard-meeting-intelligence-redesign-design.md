# Dashboard & Meeting Intelligence redesign — design

**Status:** Approved, pending implementation plan
**Date:** 2026-08-19
**Scope:** `frontend/src/components/DashboardView.tsx`, `MeetingIntelligenceOverview.tsx`,
`MeetingCard.tsx`, `MeetingRoomView.tsx`, `CreateMeetingModal.tsx`, `AppContext.tsx`,
`types/index.ts`, `services/api.ts`; `backend/app/models/meeting.py`,
`backend/app/schemas/meeting.py`, `backend/app/api/meetings.py`,
`backend/app/api/live_meeting.py` (one-line addition)

## Problem

The dashboard ([DashboardView.tsx](../../../frontend/src/components/DashboardView.tsx)) is a
plain header plus two flat lists (Upcoming / Completed). It has no concept of *how* a meeting
came to exist — a calendar invite, an ad-hoc live room, or an uploaded recording all look the
same once processed — no per-user accept/reject on invitations, and no view of "what's assigned
to me" distinct from "what meetings exist." [MeetingIntelligenceOverview.tsx](../../../frontend/src/components/MeetingIntelligenceOverview.tsx)'s
meeting grid renders every completed meeting at once with no pagination.

This spec redesigns the dashboard's hero/summary area and upcoming/task sections, adds a
lightweight invitation-card + accept/reject flow, paginates the Meeting Intelligence meeting
grid, and adds the minimal data needed to tell the three meeting origins apart on their cards.
The meeting detail page ([MeetingDetailView.tsx](../../../frontend/src/components/MeetingDetailView.tsx))
already has the right structure (Decisions / Action Items / Transcript / Memory Graph tabs) and
gets only light visual polish, not a rebuild.

## Decisions made (user-confirmed)

1. **Reject = decline for that user only.** Standard calendar-invite semantics — the meeting
   stays visible to the organizer and other invitees; only the rejecting user's own view drops
   it. (Rejected: cancelling the meeting for everyone; a purely local client-side dismiss with
   no backend record.)

2. **Live/ad-hoc meetings become processed cards automatically when the call ends** — no manual
   upload step.
   **Correction made during this design, before it went further:** the mechanism for this
   already exists and is fully wired. [live_meeting.py](../../../backend/app/api/live_meeting.py)'s
   `_create_meeting_from_session()` (line 297) already creates a real `Meeting` row when a live
   room's last participant leaves and the grace timer expires, and dispatches
   `process_live_meeting_task.delay(meeting_id)` — the same Gemini-extraction pipeline an upload
   goes through. Confirmed directly, not assumed: the router is registered
   (`app/api/__init__.py` imports and includes `live_meeting_router`), and the frontend's
   `useLiveMeetingSession` hook plus `LiveTranscriptPanel`/`LiveSuggestionBanner` are already
   imported and rendered in [MeetingRoomView.tsx](../../../frontend/src/components/MeetingRoomView.tsx)
   (lines 239, 287, 368). This was built under a separate, earlier spec
   ([2026-08-15-live-transcript-suggestions-design.md](2026-08-15-live-transcript-suggestions-design.md))
   and is unrelated to [MeetingRecorder.tsx](../../../frontend/src/components/MeetingRecorder.tsx),
   which is a *different* feature (local screen-recording download) that stays exactly as it is —
   untouched by this spec.
   **What's actually still missing:** the `Meeting` row `_create_meeting_from_session` creates
   carries no field that says "this came from a live room" — only a generated title
   (`f"Live: {room_name} — {started_at:...}"`, line 306). That gap, not the finalization
   mechanism itself, is what decision 4 below closes.

3. **Meeting scheduling + accept/reject get real backend persistence**, not the existing
   client-only localStorage pattern — chosen so an invitation one employee creates is actually
   visible to the employees they invited, across devices and logins. This is a bigger change
   than it sounds: today, [AppContext.tsx](../../../frontend/src/context/AppContext.tsx)'s
   `addMeeting()` never calls the backend at all (confirmed by reading it — it only does
   `setMeetings(prev => [newMeeting, ...prev])` plus local notification objects), so nothing
   about scheduling is actually multi-user today despite the UI implying it is. This decision
   requires rewiring `addMeeting`/`CreateMeetingModal.tsx` to call a real backend endpoint — see
   **Frontend components** below.

4. **New `source`/`room_id` tag on `Meeting`**, set once at creation, needed by both of the
   above so a card knows whether to show a meeting title or a room ID, and whether to check
   `MeetingInvite` at all.

## Architecture overview

```
 Scheduled (calendar invite)          Live (ad-hoc room)              Post-upload
 ───────────────────────────          ─────────────────────           ───────────
 CreateMeetingModal                    MeetingRoomView, typed          UploadModal /
 -> POST /meetings                     room code, LiveKit call         PostMeetingUploadModal
    { participant_names }              runs; Deepgram-live             -> POST /upload
    -> Meeting(source='scheduled',        transcription already           -> Meeting(source='upload',
         room_id=<generated>,             captures segments                 room_id=None,
         status='scheduled')              (existing feature)                status='queued')
    -> MeetingInvite row per              -> on last participant           -> process_meeting_task
         invitee (organizer:                leaving + grace timer:            .delay(...)
         'accepted', others:               _create_meeting_from_session
         'pending')                        (existing, gets ONE new
                                            line: source='live',
                                            room_id=room_name)
                                            -> process_live_meeting_task
                                               .delay(...) (existing)

         |                                       |                              |
         v                                       v                              v
   sits as an INVITATION CARD           doesn't appear on any               doesn't appear on any
   on the invitee's dashboard            dashboard until processed           dashboard until processed
   until Entered or Rejected             completes                          completes
   (POST /meetings/{id}/rsvp)
         |
         v (Enter Room accepted, or audio later attached + processed)
                              ┌───────────────────────────────┐
                              │   same completed Meeting row   │
                              │  (source tags the ORIGIN only  │
                              │   — everything downstream is   │
                              │   identical regardless of it)  │
                              └───────────────────────────────┘
                                             |
                                             v
                     GET /meetings (tagged with source/room_id/rsvp_status)
                                             |
                                             v
        Dashboard (hero counts, invitation cards, task view)  /  Meeting Intelligence
        (paginated grid, card title branches on source)       /  MeetingDetailView (unchanged)
```

## Backend components

### `Meeting` model — two new columns

[backend/app/models/meeting.py](../../../backend/app/models/meeting.py):

```python
source = Column(String, nullable=True)    # 'scheduled' | 'live' | 'upload'
room_id = Column(String, nullable=True)   # LiveKit room name; set for 'scheduled' and 'live' only
```

`room_id` uniqueness is enforced only for `source='scheduled'` (via the generator's collision
retry below) — `source='live'` rooms carry whatever ad-hoc name a participant typed, exactly as
`live_meeting.py` already allows today (its own docstring already covers repeated use of the same
room name across different calls, which is why its generated *title* embeds a timestamp). Two
different `live` meetings sharing a `room_id` is expected, not a bug; two `scheduled` meetings
never will.

Nullable so rows created before this ships (if any local dev DB already has data) don't need a
backfill — `list_meetings` and the frontend both treat a missing `source` as `'upload'`, which
was the only kind of processed meeting that existed before this feature. No migration tool exists
in this project (`app/main.py` line 18 just runs `Base.metadata.create_all(bind=engine)`, which
only creates *new* tables, not new columns on existing ones) — a local dev SQLite file created
before this change needs deleting and letting `create_all` rebuild it. Worth calling out plainly
in the implementation plan rather than discovering it as a confusing runtime error.

### New `MeetingInvite` model

Same file, next to `Meeting`. Deliberately a **new table**, not an extension of the existing
`MeetingParticipant` ([backend/app/models/employee.py](../../../backend/app/models/employee.py)
line 26) — that table's own docstring is explicit that it's "populated once at processing time
... from the AI-extracted participant list," and is the access-control source
`require_meeting_access` checks for processed meetings. Overloading it with pre-processing
invites would conflate "was invited" with "was AI-confirmed as having attended," corrupting an
existing, working invariant. `MeetingInvite` instead answers a narrower, different question —
"can this employee see this *unprocessed* meeting, and what's their RSVP" — for exactly the
window between scheduling and processing.

```python
class MeetingInvite(Base):
    __tablename__ = "meeting_invites"

    id = Column(String, primary_key=True, default=_new_id)
    meeting_id = Column(String, ForeignKey("meetings.id"), nullable=False)
    employee_id = Column(String, ForeignKey("employees.id"), nullable=False)
    rsvp_status = Column(String, nullable=False, default="pending")  # 'pending' | 'accepted' | 'declined'
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (UniqueConstraint("meeting_id", "employee_id", name="uq_invite_meeting_employee"),)
```

**Access implication, resolved below, not deferred:** once a scheduled meeting is processed,
`MeetingParticipant` is rebuilt from the AI-extracted speaker list, independent of who was
invited — an employee who accepted an invite but didn't end up in that extracted list (didn't
speak, or was misattributed) would otherwise lose visibility into the now-processed meeting, since
`require_meeting_access` today only checks `MeetingParticipant`. Caught during self-review: the
`GET /meetings` extension below already keeps such a meeting in that employee's list (via their
still-`accepted` `MeetingInvite`), which would have made clicking into it 403 — a real
list-says-yes/detail-says-no inconsistency, not a hypothetical one. Fixed by extending
`require_meeting_access` itself, not by patching each endpoint separately — see below.

### Schema changes

[backend/app/schemas/meeting.py](../../../backend/app/schemas/meeting.py):

```python
class MeetingCreate(BaseModel):
    title: str
    project: str | None = None
    date: str | None = None
    participant_names: list[str] = []
```

`participant_names`, not `participant_ids` — this codebase's established convention (see
`Employee`'s own docstring) is that **name** is the matching key across the frontend/backend
boundary, not id. Confirmed directly: frontend's `AppContext.employees` is a hardcoded local
array with fabricated ids (`emp-0`, `emp-1`, ...) that have no relationship to the backend's own
generated `Employee.id` UUIDs — only `Employee.name` (looked up case-insensitively, same as
`get_current_employee` in `auth.py` line 23) lines the two up. `CreateMeetingModal.tsx` already
lets the organizer pick from employee *names*, so this needs no new frontend lookup.

```python
class RsvpRequest(BaseModel):
    status: Literal["accepted", "declined"]
```

`MeetingListItem` gains:

```python
source: str | None = None
room_id: str | None = None
rsvp_status: str | None = None   # the CALLER's own invite status; None if they're not an invitee
```

### `POST /meetings` — extended

[backend/app/api/meetings.py](../../../backend/app/api/meetings.py) line 32. Currently takes no
`caller` dependency at all — needs one added, both to know who the organizer is (auto-accepted)
and because an anonymous-organizer meeting can't sensibly gate anyone's RSVP.

```python
@router.post("/meetings", response_model=MeetingCreateResponse)
def create_meeting(
    payload: MeetingCreate,
    db: Session = Depends(get_db),
    caller: Employee = Depends(get_current_employee),
) -> MeetingCreateResponse:
    room_id = _generate_room_code(db)  # see below
    meeting = Meeting(title=payload.title, project=payload.project, date=payload.date,
                       source="scheduled", room_id=room_id, status="scheduled")
    db.add(meeting)
    db.flush()  # need meeting.id before building invites

    invitee_names = {n.strip().lower() for n in payload.participant_names} - {caller.name.lower()}
    invitees = db.query(Employee).filter(func.lower(Employee.name).in_(invitee_names)).all()
    if len(invitees) != len(invitee_names):
        found = {e.name.lower() for e in invitees}
        raise HTTPException(400, f"Unknown participant(s): {invitee_names - found}")

    db.add(MeetingInvite(meeting_id=meeting.id, employee_id=caller.id, rsvp_status="accepted"))
    for emp in invitees:
        db.add(MeetingInvite(meeting_id=meeting.id, employee_id=emp.id, rsvp_status="pending"))

    db.commit()
    return MeetingCreateResponse(meeting_id=meeting.id)
```

Failing loudly (400) on an unrecognized name rather than silently skipping it — this is a create
endpoint, and `CreateMeetingModal.tsx`'s dropdown is already sourced from known employees, so a
mismatch here means something is actually wrong, not a benign edge case to paper over.

#### Room code generation

```python
def _generate_room_code(db: Session, attempts: int = 5) -> str:
    for _ in range(attempts):
        code = f"CORP-{secrets.token_hex(2).upper()}"  # e.g. CORP-9F3A
        if not db.query(Meeting).filter_by(room_id=code).first():
            return code
    raise HTTPException(503, "Could not generate a unique room code, please retry")
```

Deliberately a short generated code, not `meeting.id` itself (a UUID) — matches the same kind of
human-typeable room name `live_meeting.py`'s `_SAFE_ROOM` regex already expects
(`^[a-zA-Z0-9_-]{1,80}$`, confirmed at line 42), and stays usable if a participant ever needs to
join via the existing manual room-code entry screen as a fallback.

### `POST /meetings/{meeting_id}/rsvp` — new

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
        raise HTTPException(404, "No invitation found for this meeting")
    invite.rsvp_status = payload.status
    db.commit()
    return Response(status_code=204)
```

Clicking **Enter Room** sends `accepted` (there's no separate "accept without joining" step, per
the two-button spec); **Reject** sends `declined`.

### `GET /meetings` — extended access + fields

Currently (line 127) `accessible_ids` for a non-management caller is built solely from
`MeetingParticipant`. That's correct for processed meetings but means a caller couldn't see their
*own, unprocessed* invitation today even if one existed. Extend:

```python
accessible_ids: set[str] | None = None
invite_status_by_meeting: dict[str, str] = {}
if not caller.is_management:
    accessible_ids = {mp.meeting_id for mp in db.query(MeetingParticipant).filter_by(employee_id=caller.id)}
    my_invites = db.query(MeetingInvite).filter_by(employee_id=caller.id).all()
    invite_status_by_meeting = {inv.meeting_id: inv.rsvp_status for inv in my_invites}
    accessible_ids |= {mid for mid, status in invite_status_by_meeting.items() if status != "declined"}
```

...and each `MeetingListItem` gets `source=meeting.source`, `room_id=meeting.room_id`,
`rsvp_status=invite_status_by_meeting.get(meeting.id)` (management callers, or meetings the
caller isn't personally invited to, get `None`).

### `require_meeting_access` — extended

[backend/app/core/auth.py](../../../backend/app/core/auth.py) line 38 is the shared check behind
`meetings.py`'s summary/transcript/graph-data/export/delete endpoints and `graph.py`'s per-meeting
graph data — one place, reused everywhere, per its own docstring. Extend its `is_participant`
check to also count a non-declined `MeetingInvite`:

```python
def require_meeting_access(db: Session, meeting_id: str, caller: Employee) -> None:
    if caller.is_management:
        return
    has_access = (
        db.query(MeetingParticipant).filter_by(meeting_id=meeting_id, employee_id=caller.id).first() is not None
        or db.query(MeetingInvite).filter_by(meeting_id=meeting_id, employee_id=caller.id)
              .filter(MeetingInvite.rsvp_status != "declined").first() is not None
    )
    if not has_access:
        raise HTTPException(status_code=403, detail=f"Not authorized to view meeting {meeting_id}")
```

This is the one shared place to fix the list/detail inconsistency above, rather than patching
`list_meetings` and every detail endpoint separately — and it fully resolves the accepted-but-
not-AI-extracted visibility gap noted above, rather than leaving it as a known limitation.

### `POST /upload` and `live_meeting.py` — one-line tags each

- [meetings.py](../../../backend/app/api/meetings.py) line 54: `Meeting(title=meeting_title, project=project, status="queued", source="upload")`.
- [live_meeting.py](../../../backend/app/api/live_meeting.py) line 310: add `source="live", room_id=room_name` to the existing `Meeting(...)` constructor. `room_name` is already in scope as the function's own parameter — no new plumbing needed.

## Frontend components

### `types/index.ts`

`Meeting` gains `source?: 'scheduled' | 'live' | 'upload'`, `roomId?: string`,
`rsvpStatus?: 'pending' | 'accepted' | 'declined'`. `ProcessingStatus` already has `'Scheduled'`
— no new status value needed there.

### `services/api.ts`

- `STATUS_MAP` (line ~328's `mapBackendStatus`) gets one new entry: `scheduled: 'Scheduled'` —
  today it would fall through to the `'Pending'` default, visually misrepresenting an unstarted
  scheduled meeting as already mid-pipeline.
- `BackendMeetingListItem` gets `source`, `room_id`, `rsvp_status`; `mergeBackendIntoMeeting`
  passes them through onto the merged `Meeting`.
- New `scheduleMeeting(title, project, date, participantNames)` → `POST /meetings`, and
  `rsvpToMeeting(meetingId, status)` → `POST /meetings/{id}/rsvp`.

### `AppContext.tsx`

- `addMeeting()` (currently pure local state, confirmed by reading it) now calls
  `api.scheduleMeeting(...)` first; on success, pushes the *backend's* returned meeting (real id,
  `source='scheduled'`, generated `roomId`) into local state instead of a synthesized
  `mtg-${Date.now()}` placeholder. `CreateMeetingModal.tsx`'s submit handler needs no change
  beyond this — it already calls `addMeeting`.
- New `rsvpToMeeting(meetingId, status)` action: calls `api.rsvpToMeeting`, then either removes
  the meeting from local `meetings` (on `declined`) or marks it `rsvpStatus: 'accepted'` locally
  (on `accepted`, before navigating to the room).

### `DashboardView.tsx`

- Header becomes one large blue card: "Welcome back, {name}" plus three big clickable numbers —
  Upcoming / Completed / Tasks — sized to fill the card (visually similar to the unused banner
  already sitting in [MeetingDashboard.tsx](../../../frontend/src/components/MeetingDashboard.tsx),
  which is dead code — not imported anywhere in `App.tsx` — so nothing there needs preserving
  functionally, only the visual idea is worth reusing).
- Local `dashboardSection: 'upcoming' | 'tasks'` state. Clicking **Tasks** sets it to `'tasks'`;
  clicking **Upcoming** (or the Tasks stat again) sets it back. Clicking **Completed** navigates
  to Meeting Intelligence (`setActiveTab('meetings')`), same as today's "View All Cards".
  Clicking **Upcoming** additionally scrolls to that section (`scrollIntoView`) rather than
  changing tabs — it's already on this page.
- `'upcoming'` section renders the new `InvitationCard` for each meeting where
  `source === 'scheduled' && status === 'Scheduled'`, in a single column.
- `'tasks'` section: `meetings.flatMap(m => m.actionItems.map(a => ({...a, meeting: m})))`
  filtered to `assignee.toLowerCase() === currentUser.name.toLowerCase()` (same case-insensitive
  name matching this file already uses elsewhere, e.g. `openDmWithUser`), each row linking to its
  source meeting via the existing `setSelectedMeetingId` + `setActiveTab('meetings')` pattern.

### New `InvitationCard.tsx`

```tsx
interface InvitationCardProps {
  meeting: Meeting;
  onEnterRoom: (meeting: Meeting) => void;
  onReject: (meetingId: string) => void;
}
```

Title, attendees (comma-joined, matching existing card conventions), date/time. Two buttons in a
`grid grid-cols-2` (equal width by construction, matching the sizing pattern `MeetingCard.tsx`'s
existing two-button row already uses at line 133) — **Enter Room** / **Reject**, text only, no
icons, per the spec. `onEnterRoom` calls `rsvpToMeeting(id, 'accepted')` then navigates into
`MeetingRoomView` pre-joined to `meeting.roomId` (see below). `onReject` calls
`rsvpToMeeting(id, 'declined')`.

### `MeetingRoomView.tsx` — auto-join addition

Today this component only supports a participant manually typing a room code (confirmed: its
`roomName` state and join button, lines 457-490, take direct text input — nothing currently ties
it to a specific `Meeting`). Add an optional auto-join: a new `AppContext` field
(`pendingRoomJoin: { roomName: string; displayName: string } | null`), set by `InvitationCard`'s
"Enter Room" handler before switching `activeTab` to `'live-meeting'`. On mount, if set,
`MeetingRoomView` skips the manual entry screen and calls the existing `getJoinDetails` directly,
then clears it. The manual-entry path for ad-hoc rooms is untouched — this only adds a second way
to arrive at an already-known room.

### `MeetingCard.tsx` — title branches on source

```tsx
const headline = meeting.source === 'live' && meeting.roomId
  ? `Room ${meeting.roomId}`
  : meeting.title;
```

Used only for the card's own headline. `MeetingDetailView`'s header keeps using `meeting.title`
unchanged (the full, distinguishing backend-generated string) — this branching is purely a card
presentation concern, not a data change to what `title` means.

### `MeetingIntelligenceOverview.tsx` — pagination

`activeSection === 'meetings'` block (line 113 today) gets `const [page, setPage] = useState(0)`
(reset to `0` on `categoryFilter` change) and `const PAGE_SIZE = 6`. Render
`filteredCompletedMeetings.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)` in the existing
`grid-cols-1 md:grid-cols-3` layout — capping at 6 items in a 3-wide grid naturally forms two
rows without any special-casing. `<`/`>` controls either side of a "Page X of Y" label; `<`
disabled at `page === 0`, `>` disabled once `(page + 1) * PAGE_SIZE >= filteredCompletedMeetings.length`.
Client-side slicing, not a new backend query param — `meetings` is already fully fetched into
`AppContext` for the dashboard's own counts, so there's no additional fetch to paginate.

### `MeetingDetailView.tsx`

No structural change — Decisions / Action Items / Transcript / Memory Graph tabs already match
what was asked for. Visual polish only (spacing/type consistency with the redesigned dashboard),
scoped during implementation planning rather than specified line-by-line here.

## Error handling

- **RSVP on a meeting with no invite for the caller** (e.g. stale UI, direct API call) → 404,
  surfaced as a small inline error; the card is refetched/removed rather than left stuck.
- **RSVP after the meeting is already processed** — `MeetingInvite` still updates fine (it's
  independent of processing state), but by then the meeting is a completed card, not an
  invitation card, in the UI, so a stale invitation card responding late for a since-processed
  meeting simply disappears from the upcoming list rather than erroring.
- **Unknown participant name** on `POST /meetings` → 400 with the offending name(s), per above.
- **Room code collision** → retried up to 5 times server-side before failing with 503; at this
  code-space size (`16^4` per generation) collisions should be essentially never in practice.
- **Empty task list / empty upcoming list / zero completed meetings** — existing empty-state
  pattern already used throughout this codebase (e.g. `MeetingIntelligenceOverview`'s
  `EmptyState` component) reused rather than a new one invented.
- **Exactly 6 or fewer completed meetings** — pagination controls render but both are disabled
  (single page); **0 completed meetings** — existing empty state, no pagination controls shown.
- **`MeetingRoomView` auto-join fails** (server unreachable, invalid room) — falls back to the
  existing manual entry screen with the room code pre-filled, rather than a dead end.

## Testing

**Backend (automated):**
- `create_meeting` — organizer auto-accepted, invitees created `pending`, unknown name rejected
  with 400, generated `room_id` matches `_SAFE_ROOM`.
- `set_rsvp` — accepted/declined update the caller's own row only; 404 for a non-invitee.
- `list_meetings` — a pending/accepted invite makes an unprocessed meeting visible to its
  invitee and no one else (non-management); a declined one excludes it; `source`/`room_id`/
  `rsvp_status` round-trip correctly; management sees everything as today.
- `require_meeting_access` — an employee with only a non-declined `MeetingInvite` (no
  `MeetingParticipant` row yet) passes; a `declined` invite alone still 403s; existing
  `MeetingParticipant`-based access continues to pass unchanged.
- `_create_meeting_from_session` (live_meeting.py) — resulting `Meeting.source == 'live'` and
  `room_id == room_name`; existing finalization tests (title/duration generation) still pass
  unchanged.
- `POST /upload` — resulting `Meeting.source == 'upload'`.

**Frontend:** manual verification in the preview browser (this codebase has no existing
component-test setup for these views) — schedule a meeting as one demo user, switch to another
via `switchDemoUser`, confirm the invitation card appears with working Enter Room / Reject;
confirm the Tasks stat swap, scroll-to-upcoming, and Meeting Intelligence pagination (`<`/`>`,
page boundaries) all behave against real data before calling this done.

## Out of scope

- `MeetingRecorder.tsx`'s local screen-recording download — unrelated feature, unchanged.
- Knowledge/Memory Graph — confirmed already good, unchanged.
- Server-side pagination for the meeting grid — client-side slicing is sufficient at this data
  scale; revisit only if meeting counts grow far beyond a demo/hackathon scale.
- Editing/cancelling a scheduled meeting after creation, or re-inviting after a decline.
