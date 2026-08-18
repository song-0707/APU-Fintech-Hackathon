"""Standalone live-meeting smoke test.

Run from the backend folder:

    python -m app.scripts.live_meeting_smoke

What this checks:
- LiveKit token issuance.
- live-meeting WebSocket rejects bad auth.
- authenticated session accepts captions_on.
- fake Deepgram transcript is broadcast as a caption.
- transcript-so-far returns the live segment for late joiners.
- disconnect finalization dispatches the live processing task.

This intentionally fakes Deepgram and the final meeting creation so the
script is quick, repeatable, and does not create real meeting rows.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from app.api import live_meeting
from app.core.config import get_settings
from app.main import app


settings = get_settings()
client = TestClient(app, base_url="http://localhost")


@dataclass
class SmokeState:
    created_meetings: list[dict[str, Any]] = field(default_factory=list)
    dispatched_tasks: list[str] = field(default_factory=list)
    sent_audio_chunks: list[bytes] = field(default_factory=list)


class FakeDeepgramConnection:
    def __init__(self, transcript: str, state: SmokeState):
        self.results: asyncio.Queue[str] = asyncio.Queue()
        self.results.put_nowait(transcript)
        self.state = state
        self.closed = False

    async def send_audio(self, chunk: bytes) -> None:
        self.state.sent_audio_chunks.append(chunk)

    async def close(self) -> None:
        self.closed = True


class FakeTask:
    def __init__(self, state: SmokeState):
        self.state = state

    def delay(self, meeting_id: str) -> None:
        self.state.dispatched_tasks.append(meeting_id)


def _print_step(label: str) -> None:
    print(f"[live-smoke] {label}")


def _get_token(room_name: str, display_name: str = "Smoke Tester") -> str:
    response = client.post(
        "/livekit/token",
        json={"room_name": room_name, "display_name": display_name},
    )
    if response.status_code != 200:
        raise RuntimeError(f"token request failed: {response.status_code} {response.text}")
    body = response.json()
    if not body.get("token") or not body.get("identity"):
        raise AssertionError(f"token response missing fields: {body}")
    return body["token"]


async def _wait_for(condition, timeout_seconds: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.05)
    return condition()


def run() -> None:
    room_name = f"smoke-{uuid.uuid4().hex[:8]}"
    state = SmokeState()

    original_deepgram_key = settings.deepgram_api_key
    original_open_connection = live_meeting.live_transcription_service.open_connection
    original_finalize_grace = live_meeting._FINALIZE_GRACE_SECONDS
    original_create_meeting = live_meeting._create_meeting_from_session
    original_task = live_meeting.process_live_meeting_task
    original_check_text = live_meeting.contradiction_service.check_text

    async def fake_open_connection() -> FakeDeepgramConnection:
        return FakeDeepgramConnection(
            "We decided to assign the launch checklist to Smoke Tester.",
            state,
        )

    def fake_create_meeting(room: str, started_at, segments: list[dict]) -> str:
        meeting_id = "smoke-meeting-id"
        state.created_meetings.append({
            "meeting_id": meeting_id,
            "room": room,
            "segment_count": len(segments),
            "first_segment": segments[0] if segments else None,
        })
        return meeting_id

    try:
        live_meeting._sessions.pop(room_name, None)
        settings.deepgram_api_key = "fake-deepgram-key"
        live_meeting.live_transcription_service.open_connection = fake_open_connection
        live_meeting._FINALIZE_GRACE_SECONDS = 0
        live_meeting._create_meeting_from_session = fake_create_meeting
        live_meeting.process_live_meeting_task = FakeTask(state)
        live_meeting.contradiction_service.check_text = lambda *args, **kwargs: None

        _print_step("requesting LiveKit token")
        token = _get_token(room_name)

        _print_step("checking bad WebSocket auth is rejected")
        with client.websocket_connect(f"/live-meeting/{room_name}/session") as ws:
            ws.send_json({"type": "auth", "token": "not-a-real-token"})
            try:
                ws.receive_text()
                raise AssertionError("invalid token was not rejected")
            except Exception:
                pass

        _print_step("opening authenticated session and enabling captions")
        with client.websocket_connect(f"/live-meeting/{room_name}/session") as ws:
            ws.send_json({"type": "auth", "token": token})
            ws.send_json({"type": "captions_on"})

            caption = ws.receive_json()
            assert caption["type"] == "caption", caption
            assert "assign the launch checklist" in caption["text"], caption
            assert caption["speaker"] == "Smoke Tester", caption
            print(f"[live-smoke] caption received: {caption['text']}")

            late_join_response = client.get(
                f"/live-meeting/{room_name}/transcript-so-far",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert late_join_response.status_code == 200, late_join_response.text
            segments = late_join_response.json()["segments"]
            assert len(segments) == 1, segments
            assert segments[0]["text"] == caption["text"], segments

            ws.send_bytes(b"fake-audio-chunk")

        _print_step("waiting for finalization dispatch")
        finalized = asyncio.run(_wait_for(lambda: bool(state.dispatched_tasks)))
        assert finalized, "live session did not finalize"
        assert state.created_meetings[0]["room"] == room_name, state.created_meetings
        assert state.created_meetings[0]["segment_count"] == 1, state.created_meetings
        assert state.dispatched_tasks == ["smoke-meeting-id"], state.dispatched_tasks

        _print_step("PASS")
    finally:
        settings.deepgram_api_key = original_deepgram_key
        live_meeting.live_transcription_service.open_connection = original_open_connection
        live_meeting._FINALIZE_GRACE_SECONDS = original_finalize_grace
        live_meeting._create_meeting_from_session = original_create_meeting
        live_meeting.process_live_meeting_task = original_task
        live_meeting.contradiction_service.check_text = original_check_text
        live_meeting._sessions.pop(room_name, None)


if __name__ == "__main__":
    run()
