import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database.session import Base


def _new_id() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Meeting(Base):
    __tablename__ = "meetings"

    id = Column(String, primary_key=True, default=_new_id)
    title = Column(String, nullable=False)
    project = Column(String, nullable=True)
    date = Column(String, nullable=True)
    duration = Column(String, nullable=True)
    file_path = Column(String, nullable=True)
    status = Column(String, nullable=False, default="pending")
    progress = Column(Integer, nullable=False, default=0)
    source = Column(String, nullable=True)    # 'scheduled' | 'live' | 'upload'
    room_id = Column(String, nullable=True)   # LiveKit room name; set for 'scheduled' and 'live' only
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    tasks = relationship("ProcessingTask", back_populates="meeting", cascade="all, delete-orphan")


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


class ProcessingTask(Base):
    __tablename__ = "processing_tasks"

    id = Column(String, primary_key=True, default=_new_id)
    meeting_id = Column(String, ForeignKey("meetings.id"), nullable=False)
    status = Column(String, nullable=False, default="pending")
    progress = Column(Integer, nullable=False, default=0)
    error_message = Column(String, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)

    meeting = relationship("Meeting", back_populates="tasks")
