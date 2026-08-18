"""Tests for GET /meeting/{id}/export (Task 7.2).

db_session/client/caller_headers fixtures come from conftest.py — this file
used to define its own db_session/client locally; that copy moved to
conftest.py so test_dashboard.py/test_graph_api.py could share it once they
also needed an isolated DB for the new access-control dependency."""
from app.api import meetings as meetings_module
from app.models.meeting import Meeting

FAKE_SUMMARY = {
    "duration": "00:15:20",
    "summary": "The team selected Provider X for Q4.",
    "participants": ["Sarah Park", "Tom Wright"],
    "decisions": [
        {
            "title": "Switch primary vendor to Provider X",
            "confidence": "firm_commitment",
            "reason": "Best cost/SLA tradeoff.",
            "evidence": "22% savings over 36 months.",
            "timestamp": "00:11:05",
            "speaker": "Sarah Park",
        }
    ],
    "action_items": [
        {"task": "Complete security audit", "assignee": "Tom Wright", "deadline": "2026-08-20", "priority": "high"}
    ],
    "flags": [
        {"type": "contradiction", "severity": "warning", "message": "Conflicts with the May vendor freeze."}
    ],
    "risks": ["Migration window overlaps Project Alpha's Q4 deadline."],
}


def test_export_meeting_not_found_returns_404(client, db_session, caller_headers):
    response = client.get("/meeting/does-not-exist/export", headers=caller_headers)
    assert response.status_code == 404


def test_export_summary_not_ready_returns_202(client, db_session, caller_headers, monkeypatch):
    meeting = Meeting(title="Unprocessed Meeting", status="processing")
    db_session.add(meeting)
    db_session.commit()

    monkeypatch.setattr(
        meetings_module.storage, "get_file", lambda path: (_ for _ in ()).throw(FileNotFoundError())
    )

    response = client.get(f"/meeting/{meeting.id}/export", headers=caller_headers)
    assert response.status_code == 202


def test_export_returns_markdown_report_with_download_headers(client, db_session, caller_headers, monkeypatch):
    meeting = Meeting(title="Q3 Vendor Review", project="Core Infrastructure", date="2026-08-08", status="completed")
    db_session.add(meeting)
    db_session.commit()

    import json

    monkeypatch.setattr(
        meetings_module.storage, "get_file", lambda path: json.dumps(FAKE_SUMMARY)
    )

    response = client.get(f"/meeting/{meeting.id}/export", headers=caller_headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "attachment;" in response.headers["content-disposition"]
    assert "Q3_Vendor_Review" in response.headers["content-disposition"]

    body = response.text
    assert "# Q3 Vendor Review" in body
    assert "Switch primary vendor to Provider X" in body
    assert "Tom Wright" in body
    assert "Conflicts with the May vendor freeze" in body
    assert "Migration window overlaps" in body


def test_build_report_markdown_handles_empty_sections():
    meeting = Meeting(title="Empty Meeting", project=None, date=None)
    report = meetings_module._build_report_markdown(
        meeting,
        {"duration": "—", "summary": "", "participants": [], "decisions": [], "action_items": [], "flags": [], "risks": []},
    )
    assert "# Empty Meeting" in report
    assert "_No decisions recorded._" in report
    assert "_No action items recorded._" in report
