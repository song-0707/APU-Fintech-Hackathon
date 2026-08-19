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
