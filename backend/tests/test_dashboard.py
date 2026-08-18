from app.models.employee import Employee
from app.services import dashboard_service


def test_dashboard_returns_assigned_work_flags_and_recent_meetings(monkeypatch):
    def fake_run_query(cypher: str, **params):
        if "ActionItem" in cypher:
            return [{"task": "Review contract", "deadline": "2026-08-22", "priority": "high"}]
        if "CONTRADICTS" in cypher:
            return [{"message": "Conflicts with the vendor freeze", "meeting_id": "m-1"}]
        return [{"id": "m-1", "title": "Vendor Review"}]

    monkeypatch.setattr(dashboard_service, "run_query", fake_run_query)

    result = dashboard_service.get_dashboard("Sarah Park")

    assert result == {
        "user_id": "Sarah Park",
        "action_items": [{"task": "Review contract", "deadline": "2026-08-22", "priority": "high"}],
        "flags": [{"message": "Conflicts with the vendor freeze", "meeting_id": "m-1"}],
        "upcoming_meetings": [{"id": "m-1", "title": "Vendor Review"}],
    }


def test_dashboard_route_exposes_service_contract(client, db_session, caller_headers, monkeypatch):
    monkeypatch.setattr(
        dashboard_service,
        "get_dashboard",
        lambda user_id: {
            "user_id": user_id,
            "action_items": [],
            "flags": [],
            "upcoming_meetings": [],
        },
    )

    # A management caller can request anyone's dashboard — "Sarah Park" here
    # is just the contract being exercised (route -> service -> response
    # shape), not a real seeded employee.
    response = client.get("/users/Sarah%20Park/dashboard", headers=caller_headers)

    assert response.status_code == 200
    assert response.json()["user_id"] == "Sarah Park"


# ── Access control boundary (the audit's core finding: this was untested) ──

def test_dashboard_requires_identity_header(client, db_session):
    response = client.get("/users/Sarah%20Park/dashboard")
    assert response.status_code == 401


def test_dashboard_rejects_unrecognized_caller(client, db_session):
    response = client.get(
        "/users/Sarah%20Park/dashboard", headers={"X-User-Name": "Nobody Real"}
    )
    assert response.status_code == 403


def test_dashboard_employee_cannot_view_another_employees_dashboard(client, db_session):
    db_session.add_all([
        Employee(name="Alice Chen", email="alice.chen@corpbrain.ai", is_management=False),
        Employee(name="Bob Diaz", email="bob.diaz@corpbrain.ai", is_management=False),
    ])
    db_session.commit()

    response = client.get(
        "/users/Bob%20Diaz/dashboard", headers={"X-User-Name": "Alice Chen"}
    )
    assert response.status_code == 403


def test_dashboard_employee_can_view_own_dashboard(client, db_session, monkeypatch):
    db_session.add(Employee(name="Alice Chen", email="alice.chen@corpbrain.ai", is_management=False))
    db_session.commit()
    monkeypatch.setattr(
        dashboard_service,
        "get_dashboard",
        lambda user_id: {"user_id": user_id, "action_items": [], "flags": [], "upcoming_meetings": []},
    )

    response = client.get(
        "/users/Alice%20Chen/dashboard", headers={"X-User-Name": "Alice Chen"}
    )
    assert response.status_code == 200


def test_dashboard_management_can_view_any_employees_dashboard(client, db_session, caller_headers, monkeypatch):
    monkeypatch.setattr(
        dashboard_service,
        "get_dashboard",
        lambda user_id: {"user_id": user_id, "action_items": [], "flags": [], "upcoming_meetings": []},
    )

    response = client.get("/users/Anyone%20At%20All/dashboard", headers=caller_headers)
    assert response.status_code == 200
