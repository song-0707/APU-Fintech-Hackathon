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
