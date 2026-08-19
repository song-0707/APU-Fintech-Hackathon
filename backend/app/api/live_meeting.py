"""Live meeting session: room presence (auto, on joining the room) and
opt-in caption capture (this file's captions_on/off — added in the next
task) are deliberately decoupled. See "Session lifecycle" in
docs/superpowers/specs/2026-08-15-live-transcript-suggestions-design.md —
that split is what fixes the bug an earlier draft had, where toggling
captions off mid-call (or never toggling them on at all) could finalize the
meeting early or never create one at all.
"""
import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logger import get_logger
from app.database.session import SessionLocal
from app.graph import contradiction_service
from app.models.meeting import Meeting
from app.services.gemini_client import generate_content, has_gemini_credentials
from app.services.gemini_service import _clean_action_task, _has_action_verb, _is_agenda_statement
from app.services import live_transcription_service
from app.services.storage_service import StorageService
from app.tasks.meeting_tasks import process_live_meeting_task

try:
    from livekit import api as livekit_api
except ImportError:  # Keeps the rest of the API available until installed.
    livekit_api = None

logger = get_logger(__name__)
settings = get_settings()
storage = StorageService()

router = APIRouter(prefix="/live-meeting", tags=["live-meeting"])

_SAFE_ROOM = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
_FINALIZE_GRACE_SECONDS = 45
_CONTRADICTION_COOLDOWN_SECONDS = 15
_MINUTE_WINDOW_SECONDS = 60.0

# Same house style as app.services.askcoco_service._SUMMARY_KEYWORDS —
# phrase matching, not full NLP. A cheap pre-filter so contradiction checks
# (an embedding search + a judge call) only run on text that plausibly
# states a decision, not every sentence spoken in the room.
_DECISION_KEYWORDS = (
    "let's go with", "lets go with", "we'll", "we will", "decided", "decide",
    "agreed", "final", "approved", "approve", "moving forward with",
    "let's do", "lets do",
)
_ACTION_COMMITMENT_RE = re.compile(
    r"\b(action item|i will|i'll|i am going to|i'm going to|we will|we'll|"
    r"need to|needs to|please)\b",
    re.IGNORECASE,
)


def _looks_decision_like(text: str) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in _DECISION_KEYWORDS)


def _looks_action_like(text: str) -> bool:
    if _is_agenda_statement(text):
        return False
    return bool(_ACTION_COMMITMENT_RE.search(text) and _has_action_verb(text))


def _window_index(start_seconds: float) -> int:
    return max(0, int(start_seconds // _MINUTE_WINDOW_SECONDS))


def _window_bounds(minute_index: int) -> tuple[float, float]:
    start = minute_index * _MINUTE_WINDOW_SECONDS
    return start, start + _MINUTE_WINDOW_SECONDS


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60:02d}:{total % 60:02d}"


def _line_for_segment(segment: dict) -> str:
    return f"[{_format_elapsed(float(segment.get('start') or 0))}] {segment.get('speaker') or 'Speaker'}: {segment.get('text') or ''}"


def _known_speakers(segments: list[dict]) -> list[str]:
    seen: list[str] = []
    for segment in segments:
        speaker = (segment.get("speaker") or "").strip()
        if speaker and speaker not in seen:
            seen.append(speaker)
    return seen


def _guess_assignee(text: str, speaker: str, speakers: list[str]) -> str:
    lowered = text.lower()
    for name in speakers:
        if re.search(rf"\b{re.escape(name.lower())}\b\s+(?:will|to|should|needs to|need to)", lowered):
            return name
    if re.search(r"\b(i|i'll|i will|i can)\b", lowered):
        return speaker
    return speaker


class TranscriptSoFarResponse(BaseModel):
    segments: list[dict]


class LiveActionItem(BaseModel):
    task: str
    assignee: str = ""
    deadline: str = ""
    priority: str = "medium"


class LiveMinuteIntelligence(BaseModel):
    id: str
    room_name: str
    minute_index: int
    window_start: float
    window_end: float
    label: str
    summary: str
    decisions: list[str]
    action_items: list[LiveActionItem]
    risks: list[str]
    segment_count: int
    provisional: bool = True


class LiveIntelligenceSoFarResponse(BaseModel):
    segments: list[dict]
    minute_summaries: list[LiveMinuteIntelligence]


@dataclass
class LiveMeetingSession:
    room_name: str
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    segments: list[dict] = field(default_factory=list)
    minute_summaries: dict[int, LiveMinuteIntelligence] = field(default_factory=dict)
    processing_minutes: set[int] = field(default_factory=set)
    active_connections: int = 0
    last_contradiction_check: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    finalize_task: "asyncio.Task | None" = None
    minute_task: "asyncio.Task | None" = None
    connections: list[WebSocket] = field(default_factory=list)


def _fallback_minute_intelligence(room_name: str, minute_index: int, segments: list[dict]) -> LiveMinuteIntelligence:
    speakers = _known_speakers(segments)
    text_lines = [str(segment.get("text") or "").strip() for segment in segments if str(segment.get("text") or "").strip()]
    joined = " ".join(text_lines)
    start, end = _window_bounds(minute_index)

    decisions: list[str] = []
    action_items: list[LiveActionItem] = []
    risks: list[str] = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        lowered = text.lower()
        speaker = str(segment.get("speaker") or "")
        if _looks_decision_like(text):
            decisions.append(text)
        if not _looks_decision_like(text) and _looks_action_like(text):
            action_items.append(LiveActionItem(
                task=_clean_action_task(text),
                assignee=_guess_assignee(text, speaker, speakers),
                deadline="",
                priority="medium",
            ))
        if any(keyword in lowered for keyword in ("risk", "worried", "concern", "delay", "at risk", "blocked", "slip")):
            risks.append(text)

    summary = joined[:240].strip()
    if len(joined) > 240:
        summary += "..."
    if not summary:
        summary = "No transcript content captured in this window."

    return LiveMinuteIntelligence(
        id=f"{room_name}-minute-{minute_index}",
        room_name=room_name,
        minute_index=minute_index,
        window_start=start,
        window_end=end,
        label=f"{_format_elapsed(start)}-{_format_elapsed(end)}",
        summary=summary,
        decisions=decisions[:5],
        action_items=action_items[:5],
        risks=risks[:5],
        segment_count=len(segments),
    )


def _extract_minute_intelligence(room_name: str, minute_index: int, segments: list[dict]) -> LiveMinuteIntelligence:
    """Extract provisional intelligence for one completed live window.

    Gemini is used only outside demo mode and falls back to deterministic
    extraction on any failure. The final, canonical meeting intelligence is
    still generated after the room ends.
    """
    if settings.demo_mode or not has_gemini_credentials():
        return _fallback_minute_intelligence(room_name, minute_index, segments)

    start, end = _window_bounds(minute_index)
    transcript_text = "\n".join(_line_for_segment(segment) for segment in segments)
    try:
        prompt = f"""You are extracting provisional live meeting intelligence for one completed time window.
Use only the transcript window below. Do not invent facts.
Only create an action item when the transcript contains a concrete commitment
to do future work. Do not create action items from agenda/topic statements
such as "we are going to discuss the task next week".
Normalize first-person commitments into task text, e.g. "I am going to present
the project of customer feedback to CEO next week" becomes
"Present customer feedback project to CEO next week".
Return valid JSON with exactly these fields:
{{
  "summary": "one concise sentence",
  "decisions": ["decision text"],
  "action_items": [{{"task": "task text", "assignee": "speaker name if known", "deadline": "", "priority": "medium"}}],
  "risks": ["risk text"]
}}

Transcript window {minute_index} ({_format_elapsed(start)}-{_format_elapsed(end)}):
{transcript_text}
"""
        response = generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
        data = json.loads((response.text or "{}").strip())
        action_items: list[LiveActionItem] = []
        for item in (data.get("action_items") or [])[:5]:
            parsed = LiveActionItem.model_validate(item)
            parsed.task = _clean_action_task(parsed.task)
            if _has_action_verb(parsed.task) and not _is_agenda_statement(parsed.task):
                action_items.append(parsed)
        return LiveMinuteIntelligence(
            id=f"{room_name}-minute-{minute_index}",
            room_name=room_name,
            minute_index=minute_index,
            window_start=start,
            window_end=end,
            label=f"{_format_elapsed(start)}-{_format_elapsed(end)}",
            summary=str(data.get("summary") or "").strip() or "No summary extracted for this window.",
            decisions=[str(item) for item in data.get("decisions") or []][:5],
            action_items=action_items,
            risks=[str(item) for item in data.get("risks") or []][:5],
            segment_count=len(segments),
        )
    except Exception as exc:
        logger.warning("Live minute intelligence failed, using fallback: %s", exc)
        return _fallback_minute_intelligence(room_name, minute_index, segments)


_sessions: dict[str, LiveMeetingSession] = {}
_sessions_lock = asyncio.Lock()


async def _get_or_create_session(room_name: str) -> LiveMeetingSession:
    async with _sessions_lock:
        session = _sessions.get(room_name)
        if session is None:
            session = LiveMeetingSession(room_name=room_name)
            _sessions[room_name] = session
        return session


def _verify_token(token: str, room_name: str) -> tuple[str, str]:
    """Returns (identity, display_name). Raises ValueError on any failure —
    missing verifier, bad/expired signature, or a token valid for some
    *other* room."""
    if livekit_api is None:
        raise ValueError("LiveKit support is not installed on the API server")
    try:
        verifier = livekit_api.TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret)
        claims = verifier.verify(token)
    except Exception as exc:
        raise ValueError(f"Invalid or expired token: {exc}") from exc
    if not claims.video or claims.video.room != room_name:
        raise ValueError("Token is not valid for this room")
    return claims.identity, claims.name or claims.identity


def _create_meeting_from_session(room_name: str, started_at: datetime, segments: list[dict]) -> str:
    """Finalization metadata: reuses the existing nullable date/duration
    fields, and generates a distinguishing title so repeated calls in the
    same default 'team-sync' room don't all look identical in Meeting
    Intelligence. Tagged source='live'/room_id=room_name so cards can show
    the room ID as the headline instead of this generated title."""
    ended_at = datetime.now(timezone.utc)
    elapsed = max(0, int((ended_at - started_at).total_seconds()))
    h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
    duration = f"{h:02d}:{m:02d}:{s:02d}"
    title = f"Live: {room_name} — {started_at.strftime('%Y-%m-%d %H:%M')}"

    db = SessionLocal()
    try:
        meeting = Meeting(
            title=title,
            date=started_at.strftime("%Y-%m-%d %H:%M"),
            duration=duration,
            file_path=None,
            status="pending",
            source="live",
            room_id=room_name,
        )
        db.add(meeting)
        db.commit()
        db.refresh(meeting)
        meeting_id = meeting.id
    finally:
        db.close()

    storage.save_live_segments(meeting_id, segments)
    return meeting_id


async def _broadcast_minute_intelligence(session: LiveMeetingSession, item: LiveMinuteIntelligence) -> None:
    payload = {"type": "minute_intelligence", "summary": item.model_dump()}
    async with session.lock:
        connections = list(getattr(session, "connections", []))
    for ws in connections:
        try:
            await ws.send_json(payload)
        except Exception:
            continue


async def _analyze_minute_window(session: LiveMeetingSession, minute_index: int) -> LiveMinuteIntelligence | None:
    async with session.lock:
        if minute_index in session.minute_summaries or minute_index in session.processing_minutes:
            return None
        window_segments = [
            segment for segment in session.segments
            if _window_index(float(segment.get("start") or 0)) == minute_index
        ]
        if not window_segments:
            return None
        session.processing_minutes.add(minute_index)

    try:
        item = await asyncio.to_thread(_extract_minute_intelligence, session.room_name, minute_index, window_segments)
    finally:
        async with session.lock:
            session.processing_minutes.discard(minute_index)

    async with session.lock:
        if minute_index in session.minute_summaries:
            return None
        session.minute_summaries[minute_index] = item

    await _broadcast_minute_intelligence(session, item)
    return item


async def _analyze_due_minute_windows(session: LiveMeetingSession, force: bool = False) -> None:
    async with session.lock:
        if not session.segments:
            return
        if force:
            max_index = max(_window_index(float(segment.get("start") or 0)) for segment in session.segments)
        else:
            elapsed = (datetime.now(timezone.utc) - session.started_at).total_seconds()
            max_index = int(elapsed // _MINUTE_WINDOW_SECONDS) - 1
        candidates = [
            idx for idx in range(max_index + 1)
            if idx not in session.minute_summaries and idx not in session.processing_minutes
        ]

    for minute_index in candidates:
        await _analyze_minute_window(session, minute_index)


async def _minute_analysis_loop(session: LiveMeetingSession) -> None:
    while True:
        try:
            await asyncio.sleep(_MINUTE_WINDOW_SECONDS)
            await _analyze_due_minute_windows(session)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("Live minute intelligence loop failed: %s", exc)


async def _forward_deepgram_results(
    connection: "live_transcription_service.DeepgramLiveConnection",
    websocket: WebSocket,
    session: LiveMeetingSession,
    identity: str,
    display_name: str,
) -> None:
    while True:
        text = await connection.results.get()
        segment = {
            "speaker": display_name,
            "identity": identity,
            "text": text,
            "timestamp": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "start": (datetime.now(timezone.utc) - session.started_at).total_seconds(),
        }
        async with session.lock:
            session.segments.append(segment)
        await websocket.send_json({"type": "caption", **segment})

        if _looks_decision_like(text):
            now = time.monotonic()
            should_check = False
            async with session.lock:
                if now - session.last_contradiction_check > _CONTRADICTION_COOLDOWN_SECONDS:
                    session.last_contradiction_check = now
                    should_check = True
            if should_check:
                flag = await asyncio.to_thread(contradiction_service.check_text, text, exclude_meeting_id=session.id)
                if flag is not None:
                    # flag.model_dump() first: Flag has its own "type" field
                    # (FlagType — always "contradiction" here), which would
                    # otherwise silently overwrite the envelope's "type" if
                    # the spread came last.
                    await websocket.send_json({**flag.model_dump(), "type": "contradiction_suggestion"})


async def _finalize_after_grace_period(session: LiveMeetingSession) -> None:
    try:
        await asyncio.sleep(_FINALIZE_GRACE_SECONDS)
    except asyncio.CancelledError:
        return

    async with session.lock:
        if session.active_connections > 0:
            return
        segments = list(session.segments)
        started_at = session.started_at

    await _analyze_due_minute_windows(session, force=True)

    async with _sessions_lock:
        _sessions.pop(session.room_name, None)

    if session.minute_task is not None:
        session.minute_task.cancel()
        session.minute_task = None

    if not segments:
        return

    meeting_id = _create_meeting_from_session(session.room_name, started_at, segments)
    process_live_meeting_task.delay(meeting_id)
    logger.info(f"Live meeting in room '{session.room_name}' finalized as {meeting_id} ({len(segments)} segments)")


@router.websocket("/{room_name}/session")
async def live_meeting_session(websocket: WebSocket, room_name: str) -> None:
    if not _SAFE_ROOM.fullmatch(room_name):
        await websocket.close(code=4004, reason="Invalid room name")
        return

    await websocket.accept()

    try:
        first_message = await websocket.receive_json()
    except Exception:
        await websocket.close(code=4001, reason="Expected an auth message")
        return

    if first_message.get("type") != "auth" or not isinstance(first_message.get("token"), str):
        await websocket.close(code=4001, reason="First message must be {type: auth, token: ...}")
        return

    try:
        identity, display_name = _verify_token(first_message["token"], room_name)
    except ValueError as exc:
        await websocket.close(code=4001, reason=str(exc)[:120])
        return

    session = await _get_or_create_session(room_name)
    async with session.lock:
        session.active_connections += 1
        session.connections.append(websocket)
        if session.finalize_task is not None:
            session.finalize_task.cancel()
            session.finalize_task = None
        if session.minute_task is None or session.minute_task.done():
            session.minute_task = asyncio.create_task(_minute_analysis_loop(session))

    deepgram = None
    forward_task = None

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break

            if message.get("text") is not None:
                try:
                    payload = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue
                msg_type = payload.get("type")

                if msg_type == "captions_on":
                    if not settings.deepgram_api_key:
                        await websocket.send_json({"type": "captions_error", "message": "Live captions are not configured on this server."})
                        continue
                    if deepgram is None:
                        try:
                            deepgram = await live_transcription_service.open_connection()
                        except Exception as exc:
                            logger.warning(f"Failed to open Deepgram connection: {exc}")
                            await websocket.send_json({"type": "captions_error", "message": "Could not start live captions right now."})
                            continue
                        forward_task = asyncio.create_task(
                            _forward_deepgram_results(deepgram, websocket, session, identity, display_name)
                        )

                elif msg_type == "captions_off":
                    if deepgram is not None:
                        if forward_task is not None:
                            forward_task.cancel()
                            forward_task = None
                        await deepgram.close()
                        deepgram = None

            elif message.get("bytes") is not None and deepgram is not None:
                try:
                    await deepgram.send_audio(message["bytes"])
                except Exception as exc:
                    # A mid-call Deepgram drop must not crash the session WS
                    # (presence stays up) — degrade to captions_error instead.
                    logger.warning(f"Deepgram send failed, closing captions: {exc}")
                    await websocket.send_json({"type": "captions_error", "message": "Live captions disconnected."})
                    if forward_task is not None:
                        forward_task.cancel()
                        forward_task = None
                    deepgram = None
    except WebSocketDisconnect:
        pass
    finally:
        if forward_task is not None:
            forward_task.cancel()
        if deepgram is not None:
            await deepgram.close()
        async with session.lock:
            if websocket in session.connections:
                session.connections.remove(websocket)
            session.active_connections -= 1
            if session.active_connections <= 0:
                session.finalize_task = asyncio.create_task(_finalize_after_grace_period(session))


@router.get("/{room_name}/transcript-so-far", response_model=TranscriptSoFarResponse)
def get_transcript_so_far(room_name: str, authorization: str = Header(default="")) -> TranscriptSoFarResponse:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        _verify_token(token, room_name)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    session = _sessions.get(room_name)
    return TranscriptSoFarResponse(segments=list(session.segments) if session else [])


@router.get("/{room_name}/intelligence-so-far", response_model=LiveIntelligenceSoFarResponse)
def get_intelligence_so_far(room_name: str, authorization: str = Header(default="")) -> LiveIntelligenceSoFarResponse:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        _verify_token(token, room_name)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    session = _sessions.get(room_name)
    if session is None:
        return LiveIntelligenceSoFarResponse(segments=[], minute_summaries=[])
    return LiveIntelligenceSoFarResponse(
        segments=list(session.segments),
        minute_summaries=[
            session.minute_summaries[idx]
            for idx in sorted(session.minute_summaries)
        ],
    )
