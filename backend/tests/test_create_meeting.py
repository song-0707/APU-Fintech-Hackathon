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
    assert set(body["participant_names"]) == {"Owner Test", "Invitee Test"}

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
