"""Thin proxy to Deepgram's live streaming WebSocket. One connection per
participant, opened lazily on captions_on. See
docs/superpowers/specs/2026-08-15-live-transcript-suggestions-design.md.
"""
import asyncio
import json
import logging
import time

import websockets

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_DEEPGRAM_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-2&smart_format=true&interim_results=true&punctuate=true"
    "&language=en&endpointing=700&utterance_end_ms=1000&vad_events=true"
)
_KEEPALIVE_INTERVAL_SECONDS = 5
_MAX_BUFFER_WORDS = 80


class DeepgramLiveConnection:
    def __init__(self, ws) -> None:
        self._ws = ws
        self.results: asyncio.Queue[str] = asyncio.Queue()
        self._last_audio_sent = time.monotonic()
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None

    async def send_audio(self, chunk: bytes) -> None:
        await self._ws.send(chunk)
        self._last_audio_sent = time.monotonic()

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
        await self._ws.close()


async def _flush_buffer(buffer: list[str], results: "asyncio.Queue[str]") -> None:
    text = " ".join(part.strip() for part in buffer if part.strip()).strip()
    buffer.clear()
    if text:
        await results.put(text)


def _buffer_word_count(buffer: list[str]) -> int:
    return sum(len(part.split()) for part in buffer)


def _as_event(raw_data) -> dict | None:
    if isinstance(raw_data, dict):
        return raw_data
    if isinstance(raw_data, list):
        for item in raw_data:
            if isinstance(item, dict) and "channel" in item:
                return item
        for item in raw_data:
            if isinstance(item, dict) and ("channel" in item or "type" in item):
                return item
    return None


def _first_alternative(channel) -> dict:
    if not isinstance(channel, dict):
        return {}
    alternatives = channel.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        return {}
    first = alternatives[0]
    if isinstance(first, dict):
        return first
    if isinstance(first, list):
        for item in first:
            if isinstance(item, dict):
                return item
    return {}


async def _read_results(ws, results: "asyncio.Queue[str]") -> None:
    """Queue durable utterances, not every finalized ASR fragment.

    Deepgram may finalize small pieces of one sentence separately. Persisting
    each piece makes Meeting Intelligence look incomplete even when audio was
    received. Keep interim results out, buffer finalized fragments, and flush
    on utterance end or periodically during long continuous speech.
    """
    buffer: list[str] = []
    try:
        async for raw in ws:
            try:
                data = _as_event(json.loads(raw))
            except json.JSONDecodeError:
                continue
            if data is None:
                continue

            if data.get("type") == "UtteranceEnd":
                await _flush_buffer(buffer, results)
                continue

            channel = data.get("channel")
            if not channel:
                continue
            text = str(_first_alternative(channel).get("transcript", "")).strip()
            if text and data.get("is_final"):
                buffer.append(text)

            if buffer and (
                data.get("speech_final")
                or _buffer_word_count(buffer) >= _MAX_BUFFER_WORDS
            ):
                await _flush_buffer(buffer, results)

        await _flush_buffer(buffer, results)
    except Exception as exc:
        logger.warning("Deepgram live connection reader stopped: %s", exc)


async def _send_keepalive(connection: DeepgramLiveConnection) -> None:
    """Deepgram's live socket can time out during a pause in speech with no
    audio and no KeepAlive. Sends one only when audio has actually been idle
    for the interval, so it never fights with real traffic."""
    try:
        while True:
            await asyncio.sleep(_KEEPALIVE_INTERVAL_SECONDS)
            if time.monotonic() - connection._last_audio_sent >= _KEEPALIVE_INTERVAL_SECONDS:
                await connection._ws.send(json.dumps({"type": "KeepAlive"}))
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning("Deepgram keepalive stopped: %s", exc)


async def open_connection() -> DeepgramLiveConnection:
    if not settings.deepgram_api_key:
        raise ValueError("DEEPGRAM_API_KEY is not set.")
    ws = await websockets.connect(
        _DEEPGRAM_URL,
        additional_headers={"Authorization": f"Token {settings.deepgram_api_key}"},
    )
    connection = DeepgramLiveConnection(ws)
    connection._reader_task = asyncio.create_task(_read_results(ws, connection.results))
    connection._keepalive_task = asyncio.create_task(_send_keepalive(connection))
    return connection
