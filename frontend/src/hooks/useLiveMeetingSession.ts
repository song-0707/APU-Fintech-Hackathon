import { useLocalParticipant, useRoomContext } from '@livekit/components-react';
import { RoomEvent, Track } from 'livekit-client';
import { useCallback, useEffect, useRef, useState } from 'react';

export type CaptionLine = { id: string; speaker: string; text: string; timestamp: string; start?: number; end?: number };
export type LiveActionItem = { task: string; assignee: string; deadline?: string; priority?: string };
export type LiveMinuteSummary = {
  id: string;
  room_name: string;
  minute_index: number;
  window_start: number;
  window_end: number;
  label: string;
  summary: string;
  decisions: string[];
  action_items: LiveActionItem[];
  risks: string[];
  segment_count: number;
  provisional: boolean;
};
export type LiveSuggestion = {
  id: string;
  message: string;
  severity: string;
  judge: 'llm' | 'keyword_fallback';
  contradictsMeetingId?: string;
  contradictsDecisionText?: string;
};

export type LiveMeetingSessionState = {
  connectionError: string;
  captionsEnabled: boolean;
  captionsError: string;
  toggleCaptions: () => void;
  transcript: CaptionLine[];
  minuteSummaries: LiveMinuteSummary[];
  suggestions: LiveSuggestion[];
  dismissSuggestion: (id: string) => void;
};

const apiBaseUrl = (import.meta.env.VITE_API_URL as string | undefined)
  ?? `${window.location.protocol}//${window.location.hostname}:8000`;
const normalizedApiBaseUrl = apiBaseUrl.replace(/\/$/, '');
const wsBaseUrl = normalizedApiBaseUrl.replace(/^http/, 'ws');

const encoder = new TextEncoder();
const decoder = new TextDecoder();
const preferredMimeType = () => [
  'audio/webm;codecs=opus',
  'audio/webm',
].find((type) => MediaRecorder.isTypeSupported(type)) ?? '';

let localIdCounter = 0;
const nextLocalId = () => `local-${Date.now()}-${localIdCounter++}`;

export function useLiveMeetingSession(roomName: string, token: string): LiveMeetingSessionState {
  const room = useRoomContext();
  const { localParticipant, isMicrophoneEnabled } = useLocalParticipant();

  const [connectionError, setConnectionError] = useState('');
  const [captionsEnabled, setCaptionsEnabled] = useState(false);
  const [captionsError, setCaptionsError] = useState('');
  const [transcript, setTranscript] = useState<CaptionLine[]>([]);
  const [minuteSummaries, setMinuteSummaries] = useState<LiveMinuteSummary[]>([]);
  const [suggestions, setSuggestions] = useState<LiveSuggestion[]>([]);

  const wsRef = useRef<WebSocket | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);

  const publish = useCallback((topic: 'live-transcript' | 'live-suggestion' | 'live-minute-intelligence', payload: unknown) => {
    void room.localParticipant.publishData(encoder.encode(JSON.stringify(payload)), { reliable: true, topic });
  }, [room]);

  const upsertMinuteSummary = useCallback((summary: LiveMinuteSummary) => {
    setMinuteSummaries((prev) => {
      const existing = prev.findIndex((item) => item.id === summary.id || item.minute_index === summary.minute_index);
      if (existing === -1) return [...prev, summary].sort((a, b) => a.minute_index - b.minute_index);
      const next = [...prev];
      next[existing] = summary;
      return next.sort((a, b) => a.minute_index - b.minute_index);
    });
  }, []);

  // Session WS: opens once on room join, independent of the caption toggle.
  useEffect(() => {
    if (!token) return;
    // React StrictMode deliberately mounts every effect twice in dev
    // (mount -> cleanup -> mount again) to surface missing cleanup bugs.
    // The first WebSocket instance gets closed by that cleanup before it
    // ever opens, firing onerror/onclose for a connection that was never
    // really broken. Clearing any stale error here, at the start of each
    // new attempt, means a torn-down previous instance can never leave a
    // permanent error banner in front of a second instance that connects
    // fine — this isn't a StrictMode-only concern, the same reasoning
    // applies to any future reconnect attempt too.
    setConnectionError('');
    const ws = new WebSocket(`${wsBaseUrl}/live-meeting/${roomName}/session`);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnectionError('');
      ws.send(JSON.stringify({ type: 'auth', token }));
      ws.send(JSON.stringify({ type: 'captions_on' }));
      setCaptionsEnabled(true);
    };

    ws.onmessage = (event) => {
      const payload = JSON.parse(event.data as string);
      if (payload.type === 'caption') {
        const line: CaptionLine = {
          id: nextLocalId(),
          speaker: payload.speaker,
          text: payload.text,
          timestamp: payload.timestamp,
          start: payload.start,
          end: payload.end,
        };
        setTranscript((prev) => [...prev, line]);
        publish('live-transcript', line);
      } else if (payload.type === 'contradiction_suggestion') {
        const suggestion: LiveSuggestion = {
          id: nextLocalId(),
          message: payload.message,
          severity: payload.severity,
          judge: payload.judge,
          contradictsMeetingId: payload.contradicts_meeting_id,
          contradictsDecisionText: payload.contradicts_decision_text,
        };
        setSuggestions((prev) => [...prev, suggestion]);
        publish('live-suggestion', suggestion);
      } else if (payload.type === 'minute_intelligence' && payload.summary) {
        upsertMinuteSummary(payload.summary);
        publish('live-minute-intelligence', payload.summary);
      } else if (payload.type === 'captions_error') {
        setCaptionsError(payload.message);
        setCaptionsEnabled(false);
      }
    };

    ws.onerror = () => setConnectionError('Could not reach the live meeting service.');
    ws.onclose = (event) => {
      if (event.code >= 4000) setConnectionError(event.reason || 'The live meeting connection was closed.');
    };

    return () => ws.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [publish, roomName, token, upsertMinuteSummary]);

  // History hydration for a late joiner — the LiveKit data channel has no
  // replay of its own, so this is a one-time backend read on mount.
  useEffect(() => {
    if (!token) return;
    fetch(`${normalizedApiBaseUrl}/live-meeting/${roomName}/intelligence-so-far`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((res) => (res.ok ? res.json() : { segments: [], minute_summaries: [] }))
      .then((data: {
        segments: Array<{ speaker: string; text: string; timestamp: string; start?: number; end?: number }>;
        minute_summaries: LiveMinuteSummary[];
      }) => {
        setTranscript((prev) => [
          ...data.segments.map((s) => ({
            id: nextLocalId(),
            speaker: s.speaker,
            text: s.text,
            timestamp: s.timestamp,
            start: s.start,
            end: s.end,
          })),
          ...prev,
        ]);
        data.minute_summaries.forEach(upsertMinuteSummary);
      })
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [roomName, token, upsertMinuteSummary]);

  // Receiving other participants' captions/suggestions via the same
  // LiveKit data-channel pattern CollaborativeWhiteboard.tsx already uses.
  useEffect(() => {
    const handleMessage = (data: Uint8Array, _participant?: unknown, _kind?: unknown, topic?: string) => {
      if (topic !== 'live-transcript' && topic !== 'live-suggestion' && topic !== 'live-minute-intelligence') return;
      const payload = JSON.parse(decoder.decode(data));
      if (topic === 'live-transcript') {
        setTranscript((prev) => (prev.some((line) => line.id === payload.id) ? prev : [...prev, payload]));
      } else if (topic === 'live-suggestion') {
        setSuggestions((prev) => (prev.some((s) => s.id === payload.id) ? prev : [...prev, payload]));
      } else {
        upsertMinuteSummary(payload);
      }
    };
    room.on(RoomEvent.DataReceived, handleMessage);
    return () => { room.off(RoomEvent.DataReceived, handleMessage); };
  }, [room, upsertMinuteSummary]);

  const stopCapture = useCallback(() => {
    if (recorderRef.current?.state !== 'inactive') recorderRef.current?.stop();
    recorderRef.current = null;
  }, []);

  const toggleCaptions = useCallback(() => {
    setCaptionsEnabled((enabled) => {
      const next = !enabled;
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return enabled;

      if (next) {
        setCaptionsError('');
        ws.send(JSON.stringify({ type: 'captions_on' }));
      } else {
        ws.send(JSON.stringify({ type: 'captions_off' }));
        stopCapture();
      }
      return next;
    });
  }, [stopCapture]);

  const localMicTrack = localParticipant.getTrackPublication(Track.Source.Microphone)?.track;
  const micTrackSid = localMicTrack?.sid;

  // Must respect LiveKit mute state: stop sending audio while muted, but
  // keep the transcript session armed so capture resumes when the mic does.
  useEffect(() => {
    if (!captionsEnabled) return;
    if (!isMicrophoneEnabled) {
      stopCapture();
      return;
    }

    const track = localMicTrack?.mediaStreamTrack;
    const ws = wsRef.current;
    if (!track || !ws || ws.readyState !== WebSocket.OPEN) return;

    const mimeType = preferredMimeType();
    const recorder = new MediaRecorder(new MediaStream([track]), mimeType ? { mimeType } : undefined);
    recorder.ondataavailable = (event) => {
      if (event.data.size > 0 && ws.readyState === WebSocket.OPEN) void event.data.arrayBuffer().then((buf) => ws.send(buf));
    };
    recorder.start(1_000);
    recorderRef.current = recorder;

    return () => {
      if (recorder.state !== 'inactive') recorder.stop();
      if (recorderRef.current === recorder) recorderRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [captionsEnabled, isMicrophoneEnabled, micTrackSid, stopCapture]);

  const dismissSuggestion = useCallback((id: string) => {
    setSuggestions((prev) => prev.filter((s) => s.id !== id));
  }, []);

  return { connectionError, captionsEnabled, captionsError, toggleCaptions, transcript, minuteSummaries, suggestions, dismissSuggestion };
}
