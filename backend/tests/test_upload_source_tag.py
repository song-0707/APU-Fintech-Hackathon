import io

from app.models.meeting import Meeting


def test_upload_tags_meeting_source_as_upload(client, db_session, monkeypatch):
    from app.tasks import meeting_tasks
    monkeypatch.setattr(meeting_tasks.process_meeting_task, "delay", lambda meeting_id: None)

    response = client.post(
        "/upload",
        files={"file": ("recording.mp3", io.BytesIO(b"fake audio bytes"), "audio/mpeg")},
        data={"title": "Uploaded Sync"},
    )

    assert response.status_code == 202
    meeting_id = response.json()["meeting_id"]
    meeting = db_session.query(Meeting).filter_by(id=meeting_id).first()
    assert meeting.source == "upload"
