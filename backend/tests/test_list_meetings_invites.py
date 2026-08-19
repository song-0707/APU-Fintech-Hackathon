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
    assert item["participant_names"] == ["List Invitee"]


def test_declined_invite_excludes_meeting_from_list(client, db_session):
    invitee = Employee(name="Declined Invitee", email="declined.invitee@corpbrain.ai")
    meeting = Meeting(title="Roadmap Sync 2", source="scheduled", room_id="CORP-LM02", status="scheduled")
    db_session.add_all([invitee, meeting])
    db_session.commit()
    db_session.add(MeetingInvite(meeting_id=meeting.id, employee_id=invitee.id, rsvp_status="declined"))
    db_session.commit()

    response = client.get("/meetings", headers={"X-User-Name": invitee.name})

    assert meeting.id not in {item["id"] for item in response.json()}


def test_management_caller_also_sees_own_rsvp_status_and_declined_exclusion(client, db_session):
    """Every demo employee in this app is management-flagged (see
    app/main.py's _DEMO_EMPLOYEES), so a management caller's own RSVP must
    behave identically to a non-management one, not just their broader
    see-everything access. Regression test for a bug where invite_status_by_meeting
    was only ever populated inside the `if not caller.is_management` branch."""
    manager = Employee(name="Management Invitee", email="management.invitee@corpbrain.ai", is_management=True)
    pending_meeting = Meeting(title="Mgmt Pending", source="scheduled", room_id="CORP-MG01", status="scheduled")
    declined_meeting = Meeting(title="Mgmt Declined", source="scheduled", room_id="CORP-MG02", status="scheduled")
    db_session.add_all([manager, pending_meeting, declined_meeting])
    db_session.commit()
    db_session.add(MeetingInvite(meeting_id=pending_meeting.id, employee_id=manager.id, rsvp_status="pending"))
    db_session.add(MeetingInvite(meeting_id=declined_meeting.id, employee_id=manager.id, rsvp_status="declined"))
    db_session.commit()

    response = client.get("/meetings", headers={"X-User-Name": manager.name})
    items = response.json()
    ids = {item["id"] for item in items}

    assert pending_meeting.id in ids
    assert declined_meeting.id not in ids
    pending_item = next(item for item in items if item["id"] == pending_meeting.id)
    assert pending_item["rsvp_status"] == "pending"
