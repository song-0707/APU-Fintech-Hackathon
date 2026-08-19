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
