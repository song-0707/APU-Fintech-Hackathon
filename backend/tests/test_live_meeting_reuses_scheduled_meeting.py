import uuid
from datetime import datetime, timezone

from app.api import live_meeting
from app.database.session import SessionLocal
from app.models.meeting import Meeting


def test_finalization_reuses_the_original_scheduled_meeting_row():
    # No hardcoded room name: this suite runs against a real, non-isolated
    # SQLite database (see test_phase_contracts.py's identical note), so a
    # fixed value can collide with a row a previous run left behind.
    room_name = f"test-reuse-{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    scheduled = Meeting(
        title="Real Scheduled Title", project="Test Project",
        source="scheduled", room_id=room_name, status="scheduled",
    )
    db.add(scheduled)
    db.commit()
    scheduled_id = scheduled.id
    db.close()

    started_at = datetime.now(timezone.utc)
    meeting_id = live_meeting._create_meeting_from_session(room_name, started_at, [])

    assert meeting_id == scheduled_id

    db = SessionLocal()
    try:
        reused = db.query(Meeting).filter_by(id=meeting_id).first()
        assert reused.title == "Real Scheduled Title"
        assert reused.project == "Test Project"
        assert reused.source == "scheduled"
        assert reused.room_id == room_name
        assert reused.status == "pending"
        assert reused.date is not None
        assert reused.duration is not None
        assert db.query(Meeting).filter_by(room_id=room_name).count() == 1
    finally:
        db.close()


def test_finalization_creates_a_new_meeting_when_no_scheduled_row_matches():
    room_name = f"test-adhoc-{uuid.uuid4().hex[:8]}"
    started_at = datetime.now(timezone.utc)

    meeting_id = live_meeting._create_meeting_from_session(room_name, started_at, [])

    db = SessionLocal()
    try:
        created = db.query(Meeting).filter_by(id=meeting_id).first()
        assert created is not None
        assert created.source == "live"
        assert created.room_id == room_name
        assert created.title.startswith(f"Live: {room_name}")
    finally:
        db.close()


def test_finalization_does_not_reuse_an_already_processed_scheduled_meeting():
    # Guards against silently clobbering already-processed decisions/
    # transcript if the same room code is ever reused after the meeting
    # it originally belonged to has already gone through the pipeline.
    room_name = f"test-processed-{uuid.uuid4().hex[:8]}"
    db = SessionLocal()
    already_processed = Meeting(
        title="Already Done", source="scheduled", room_id=room_name, status="completed",
    )
    db.add(already_processed)
    db.commit()
    already_processed_id = already_processed.id
    db.close()

    started_at = datetime.now(timezone.utc)
    meeting_id = live_meeting._create_meeting_from_session(room_name, started_at, [])

    assert meeting_id != already_processed_id

    db = SessionLocal()
    try:
        new_row = db.query(Meeting).filter_by(id=meeting_id).first()
        assert new_row.source == "live"
        old_row = db.query(Meeting).filter_by(id=already_processed_id).first()
        assert old_row.status == "completed"
        assert old_row.title == "Already Done"
    finally:
        db.close()
