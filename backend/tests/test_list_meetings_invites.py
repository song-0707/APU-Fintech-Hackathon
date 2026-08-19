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
