import {
  LiveKitRoom,
  RoomAudioRenderer,
  useLocalParticipant,
  useParticipants,
  useTracks,
  VideoTrack,
} from '@livekit/components-react';
import { Track } from 'livekit-client';
import {
  Camera,
  CameraOff,
  AlertCircle,
  Captions,
  LogOut,
  Maximize2,
  Mic,
  MicOff,
  Minimize2,
  MonitorUp,
  Send,
  Sparkles,
  Users,
  Video,
} from 'lucide-react';
import React, { useEffect, useRef, useState } from 'react';
import { useApp } from '../context/AppContext';
import { LiveMinuteSummary, useLiveMeetingSession } from '../hooks/useLiveMeetingSession';
import { askCoco, BackendCitation } from '../services/api';
import { CollaborativeWhiteboard } from './CollaborativeWhiteboard';
import { LiveSuggestionBanner } from './LiveSuggestionBanner';
import { LiveTranscriptPanel } from './LiveTranscriptPanel';
import { MeetingRecorder } from './MeetingRecorder';

type JoinDetails = { token: string; serverUrl: string; roomName: string; displayName: string };

// With no explicit deployment URL, use the same machine that served Vite.
// This makes a LAN URL such as http://192.168.1.20:5173 call that host's API
// instead of incorrectly calling localhost on the guest's computer.
// Vite can be opened on IPv6 loopback while FastAPI is bound to IPv4 during
// local development, so route that one local-only case to the IPv4 loopback.
const frontendHost = window.location.hostname;
const apiHost = frontendHost === '::1' || frontendHost === '[::1]'
  ? '127.0.0.1'
  : frontendHost;
const apiBaseUrl = import.meta.env.VITE_API_URL
  ?? `${window.location.protocol}//${apiHost}:8000`;

async function getJoinDetails(roomName: string, displayName: string): Promise<JoinDetails> {
  let response: Response;
  try {
    response = await fetch(`${apiBaseUrl}/livekit/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room_name: roomName, display_name: displayName }),
    });
  } catch {
    throw new Error(`Cannot reach the meeting server at ${apiBaseUrl}. Start FastAPI on port 8000, then try again.`);
  }

  let payload: { token?: string; server_url?: string; detail?: string } = {};
  try {
    payload = await response.json() as typeof payload;
  } catch {
    if (!response.ok) throw new Error(`Meeting server returned ${response.status}. Check that FastAPI is running correctly.`);
  }
  if (!response.ok || !payload.token || !payload.server_url) {
    throw new Error(payload.detail ?? 'Unable to create a secure room token.');
  }
  return { token: payload.token, serverUrl: payload.server_url, roomName, displayName };
}

const ToggleButton: React.FC<{
  label: string;
  enabled: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  inactiveIcon: React.ReactNode;
}> = ({ label, enabled, onClick, icon, inactiveIcon }) => (
  <button onClick={onClick} className={`flex min-w-20 flex-col items-center gap-1 rounded-xl px-3 py-2 text-xs font-semibold transition-colors ${enabled ? 'bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-700' : 'bg-rose-50 dark:bg-rose-950/40 text-rose-700 dark:text-rose-300 hover:bg-rose-100 dark:hover:bg-rose-900/50'}`}>
    {enabled ? icon : inactiveIcon}<span>{label}</span>
  </button>
);

type CocoMessage = { role: 'user' | 'coco'; text: string; citations?: BackendCitation[] };

const wantsLiveContext = (query: string) => {
  const lowered = query.toLowerCase();
  return [
    'what did i miss', 'missed', 'before i joined', 'just discussed',
    'live so far', 'so far', 'current meeting', 'this meeting',
  ].some((phrase) => lowered.includes(phrase));
};

const liveAnswer = (query: string, liveSummaries: LiveMinuteSummary[], missedSummaries: LiveMinuteSummary[]) => {
  const lowered = query.toLowerCase();
  const source = lowered.includes('miss') || lowered.includes('before i joined')
    ? missedSummaries
    : lowered.includes('just') || lowered.includes('recent')
    ? liveSummaries.slice(-1)
    : liveSummaries;

  if (source.length === 0) {
    return {
      answer: 'I do not have enough live meeting context yet. Keep the microphone unmuted and wait for the first minute summary.',
      citations: [],
    };
  }

  const decisions = source.flatMap((item) => item.decisions.map((decision) => `${item.label}: ${decision}`));
  const actions = source.flatMap((item) => item.action_items.map((action) => `${item.label}: ${action.task}${action.assignee ? ` — ${action.assignee}` : ''}`));
  const risks = source.flatMap((item) => item.risks.map((risk) => `${item.label}: ${risk}`));
  const summaryLines = source.map((item) => `${item.label}: ${item.summary}`);
  const sections = [
    summaryLines.join('\n'),
    decisions.length ? `\nDecisions:\n${decisions.slice(0, 5).join('\n')}` : '',
    actions.length ? `\nAction items:\n${actions.slice(0, 5).join('\n')}` : '',
    risks.length ? `\nRisks:\n${risks.slice(0, 5).join('\n')}` : '',
  ].filter(Boolean);

  return {
    answer: sections.join('\n'),
    citations: source.slice(-3).map((item) => ({
      filename: `Live meeting ${item.label}`,
      speaker: 'Live minute intelligence',
      timestamp: item.label,
      excerpt: item.summary,
    })),
  };
};

// Historical questions use the same secured POST /query backend as
// CocoChatView; live-meeting catch-up questions are answered from the
// room's provisional minute summaries.
const CocoPanel: React.FC<{
  minuteSummaries: LiveMinuteSummary[];
  missedMinuteSummaries: LiveMinuteSummary[];
}> = ({ minuteSummaries, missedMinuteSummaries }) => {
  const [messages, setMessages] = useState<CocoMessage[]>([]);
  const [input, setInput] = useState('');
  const [isAsking, setIsAsking] = useState(false);

  const send = async () => {
    const query = input.trim();
    if (!query || isAsking) return;
    setInput('');
    setMessages((prev) => [...prev, { role: 'user', text: query }]);
    setIsAsking(true);
    try {
      const result = wantsLiveContext(query)
        ? liveAnswer(query, minuteSummaries, missedMinuteSummaries)
        : await askCoco(query);
      setMessages((prev) => [...prev, { role: 'coco', text: result.answer, citations: result.citations }]);
    } catch {
      setMessages((prev) => [...prev, { role: 'coco', text: "I couldn't reach the Ask Coco backend just now." }]);
    } finally {
      setIsAsking(false);
    }
  };

  return (
    <section className="rounded-2xl border border-violet-200 dark:border-violet-900/60 bg-violet-50/40 dark:bg-violet-950/30 p-4 space-y-3">
      <div className="flex items-center gap-2 text-sm font-bold text-violet-700 dark:text-violet-300">
        <Sparkles className="h-4 w-4" /> Ask Coco
        <span className="font-normal text-violet-500 dark:text-violet-400">— live and past meeting context</span>
      </div>

      <div className="max-h-64 overflow-y-auto space-y-2.5 pr-1">
        {messages.length === 0 && (
          <p className="text-xs text-slate-400">
            Ask what you missed, what was just discussed, or about past decisions without leaving the call.
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={m.role === 'user' ? 'text-right' : 'text-left'}>
            <div
              className={`inline-block max-w-[92%] rounded-xl px-3 py-2 text-xs whitespace-pre-wrap text-left ${
                m.role === 'user'
                  ? 'bg-blue-600 text-white'
                  : 'bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-700 text-slate-700 dark:text-slate-300'
              }`}
            >
              {m.text}
            </div>
            {m.citations && m.citations.length > 0 && (
              <div className="mt-1 space-y-0.5 text-left">
                {m.citations.slice(0, 3).map((c, ci) => (
                  <div key={ci} className="text-[10px] text-slate-400">
                    📎 {c.filename}
                    {c.speaker ? ` — ${c.speaker}` : ''}: "{c.excerpt.length > 70 ? `${c.excerpt.slice(0, 70)}…` : c.excerpt}"
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
        {isAsking && <div className="text-xs text-slate-400 animate-pulse">Coco is thinking…</div>}
      </div>

      <div className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') send();
          }}
          placeholder="Ask a question…"
          className="flex-1 rounded-xl border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-900 dark:text-white px-3 py-2 text-xs outline-none focus:border-blue-600 dark:focus:border-blue-500"
        />
        <button
          onClick={() => void send()}
          disabled={isAsking || !input.trim()}
          className="flex items-center gap-1.5 rounded-xl bg-blue-600 px-3 py-2 text-xs font-bold text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <Send className="h-3.5 w-3.5" /> Ask
        </button>
      </div>
    </section>
  );
};

const RoomContent: React.FC<{
  roomName: string;
  displayName: string;
  token: string;
  isFullscreen: boolean;
  onLeave: () => void;
  onToggleFullscreen: () => Promise<void>;
}> = ({ roomName, displayName, token, isFullscreen, onLeave, onToggleFullscreen }) => {
  const participants = useParticipants();
  const cameraTracks = useTracks([Track.Source.Camera]);
  const screenTracks = useTracks([Track.Source.ScreenShare]);
  const { localParticipant, isCameraEnabled, isMicrophoneEnabled, isScreenShareEnabled } = useLocalParticipant();
  const [mediaError, setMediaError] = useState('');
  const [showCoco, setShowCoco] = useState(false);
  const [showTranscript, setShowTranscript] = useState(false);
  const [isScreenEnlarged, setIsScreenEnlarged] = useState(false);
  const [isProcessingPipeline, setIsProcessingPipeline] = useState(false);
  const liveSession = useLiveMeetingSession(roomName, token);
  const meetingServiceError = mediaError || liveSession.captionsError || liveSession.connectionError;

  const toggle = async (kind: 'microphone' | 'camera' | 'screen') => {
    try {
      setMediaError('');
      if (kind === 'microphone') await localParticipant.setMicrophoneEnabled(!isMicrophoneEnabled);
      if (kind === 'camera') await localParticipant.setCameraEnabled(!isCameraEnabled);
      if (kind === 'screen') await localParticipant.setScreenShareEnabled(!isScreenShareEnabled);
    } catch (error) {
      setMediaError(error instanceof Error ? error.message : 'Permission was not granted for this device.');
    }
  };

  const handleLeaveMeeting = () => {
    if (isProcessingPipeline) return;
    setIsProcessingPipeline(true);
    window.setTimeout(() => {
      setIsProcessingPipeline(false);
      onLeave();
    }, 900);
  };

  return (
    <div data-meeting-recording-area className="space-y-5 p-4 pb-28 sm:p-6 sm:pb-28">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2">
          </div>
          <div className="mt-1 flex items-center gap-2">
            <h1 className="text-2xl font-extrabold tracking-tight text-slate-900 dark:text-white font-sans">Room Code: {roomName}</h1>
            <button
              type="button"
              onClick={() => {
                navigator.clipboard.writeText(roomName);
                alert(`Room Code "${roomName}" copied to clipboard! Share this exact code with teammates to join this session.`);
              }}
              className="px-2.5 py-1 text-xs bg-blue-50 dark:bg-blue-950/60 hover:bg-blue-100 dark:hover:bg-blue-900/60 text-blue-600 dark:text-blue-400 font-bold rounded-lg border border-blue-200 dark:border-blue-900 transition-colors cursor-pointer"
            >
              Copy Room Code
            </button>
          </div>
        </div>
        <span className="flex items-center gap-1.5 rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 px-3 py-2 text-xs font-semibold text-slate-600 dark:text-slate-400"><Users className="h-4 w-4 text-indigo-600 dark:text-indigo-400" />{participants.length} participant{participants.length === 1 ? '' : 's'}</span>
      </div>

      {meetingServiceError && <div className="flex items-start gap-2 rounded-xl border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 p-3 text-sm text-amber-800 dark:text-amber-300"><AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />{meetingServiceError}</div>}

      <LiveSuggestionBanner suggestions={liveSession.suggestions} onDismiss={liveSession.dismissSuggestion} />

      <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_270px]">
        <div className="space-y-4">
          {screenTracks.length > 0 && (
            <section className="relative overflow-hidden rounded-2xl border border-blue-200 dark:border-blue-900 bg-slate-950 shadow-sm group">
              <div className="flex items-center justify-between bg-blue-600 px-3 py-2 text-xs font-bold text-white">
                <div className="flex items-center gap-2">
                  <MonitorUp className="h-4 w-4" />
                  <span>Screen Share ({screenTracks[0].participant.name || 'Participant'})</span>
                </div>
              </div>

              <div className="relative w-full flex items-center justify-center">
                <VideoTrack trackRef={screenTracks[0]} className="max-h-[52vh] w-full object-contain" />

                {/* Bottom Right Corner Enlarge / Fullscreen Button */}
                <button
                  type="button"
                  onClick={() => setIsScreenEnlarged(!isScreenEnlarged)}
                  title={isScreenEnlarged ? "Exit Fullscreen" : "Enlarge Screen Share"}
                  className="absolute bottom-3 right-3 z-10 flex items-center gap-1.5 rounded-xl bg-slate-900/90 hover:bg-blue-600 text-white px-3 py-2 text-xs font-bold shadow-lg backdrop-blur-md transition-all hover:scale-105 border border-white/20 cursor-pointer"
                >
                  <Maximize2 className="h-4 w-4 text-blue-400 group-hover:text-white" />
                  <span>Enlarge Screen</span>
                </button>
              </div>
            </section>
          )}

          {/* Fullscreen Overlay when Enlarged */}
          {isScreenEnlarged && screenTracks.length > 0 && (
            <div className="fixed inset-0 z-50 flex flex-col bg-slate-950/95 p-4 sm:p-6 animate-fade-in backdrop-blur-md">
              <div className="flex items-center justify-between pb-3 border-b border-slate-800 text-white shrink-0">
                <div className="flex items-center gap-2 text-sm font-bold font-sans">
                  <MonitorUp className="h-5 w-5 text-blue-400" />
                  <span>Screen Share — Fullscreen View ({roomName})</span>
                </div>
                <button
                  type="button"
                  onClick={() => setIsScreenEnlarged(false)}
                  className="px-3.5 py-2 bg-blue-600 hover:bg-blue-700 text-white text-xs font-bold rounded-xl flex items-center gap-1.5 shadow-md cursor-pointer transition-all"
                >
                  <Minimize2 className="h-4 w-4" />
                  <span>Exit Fullscreen</span>
                </button>
              </div>
              <div className="flex-1 flex items-center justify-center overflow-hidden p-2 relative">
                <VideoTrack trackRef={screenTracks[0]} className="max-h-[88vh] max-w-full rounded-2xl object-contain shadow-2xl" />

                <button
                  type="button"
                  onClick={() => setIsScreenEnlarged(false)}
                  className="absolute bottom-4 right-4 z-10 flex items-center gap-1.5 rounded-xl bg-blue-600 hover:bg-blue-700 text-white px-4 py-2.5 text-xs font-bold shadow-xl transition-all hover:scale-105 border border-white/20 cursor-pointer"
                >
                  <Minimize2 className="h-4 w-4" />
                  <span>Minimize</span>
                </button>
              </div>
            </div>
          )}
          <section className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-3 shadow-sm">
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 2xl:grid-cols-3">
              {cameraTracks.length > 0 ? cameraTracks.map((track) => (
                <div key={`${track.participant.identity}-${track.publication.trackSid}`} className="relative aspect-video overflow-hidden rounded-xl bg-slate-900">
                  <VideoTrack trackRef={track} className="h-full w-full object-cover" />
                  <span className="absolute bottom-2 left-2 rounded-md bg-slate-950/70 px-2 py-1 text-xs font-semibold text-white">{track.participant.name || track.participant.identity}</span>
                </div>
              )) : (
                <div className="col-span-full grid min-h-52 place-items-center rounded-xl border border-dashed border-slate-300 dark:border-slate-700 bg-slate-50 dark:bg-slate-800/60 text-sm text-slate-500 dark:text-slate-400">Turn on your camera to start the video grid.</div>
              )}
            </div>
          </section>
          <CollaborativeWhiteboard roomName={roomName} />
          {showCoco && (
            <CocoPanel
              minuteSummaries={liveSession.minuteSummaries}
              missedMinuteSummaries={liveSession.missedMinuteSummaries}
            />
          )}
          {showTranscript && (
            <LiveTranscriptPanel
              transcript={liveSession.transcript}
              minuteSummaries={liveSession.minuteSummaries}
              error={liveSession.captionsError || liveSession.connectionError}
            />
          )}
        </div>

        <aside className="h-fit rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-4 shadow-sm xl:sticky xl:top-4">
          <h2 className="mb-3 flex items-center gap-2 text-sm font-bold text-slate-800 dark:text-slate-200"><Users className="h-4 w-4 text-blue-600 dark:text-blue-400" />Participants</h2>
          <ul className="space-y-2">
            {participants.map((participant) => (
              <li key={participant.identity} className="flex items-center justify-between gap-2 rounded-xl bg-slate-50 dark:bg-slate-800/60 px-3 py-2">
                <div className="min-w-0"><p className="truncate text-xs font-bold text-slate-700 dark:text-slate-300">{participant.name || participant.identity}</p><p className="text-[10px] text-slate-400">{participant.isSpeaking ? 'Speaking…' : 'In meeting'}</p></div>
                <div className="flex items-center gap-1.5">{participant.isSpeaking && <span className="h-2 w-2 animate-pulse rounded-full bg-emerald-500" />}{participant.isMicrophoneEnabled ? <Mic className="h-3.5 w-3.5 text-slate-500 dark:text-slate-400" /> : <MicOff className="h-3.5 w-3.5 text-rose-500" />}{participant.isCameraEnabled ? <Camera className="h-3.5 w-3.5 text-slate-500 dark:text-slate-400" /> : <CameraOff className="h-3.5 w-3.5 text-rose-500" />}</div>
              </li>
            ))}
          </ul>
        </aside>
      </div>

      <div className={`fixed bottom-3 z-40 flex w-max flex-nowrap items-center justify-center gap-2 overflow-visible rounded-2xl border border-slate-200 dark:border-slate-800 bg-white/95 dark:bg-slate-900/95 p-2 shadow-lg backdrop-blur ${
        isFullscreen
          ? 'left-1/2 max-w-[calc(100%-1.5rem)] -translate-x-1/2'
          : 'left-[calc(50%+9rem)] max-w-[calc(100%-19.5rem)] -translate-x-1/2'
      }`}>
        <ToggleButton label="Mic" enabled={isMicrophoneEnabled} onClick={() => void toggle('microphone')} icon={<Mic className="h-5 w-5" />} inactiveIcon={<MicOff className="h-5 w-5" />} />
        <ToggleButton label="Camera" enabled={isCameraEnabled} onClick={() => void toggle('camera')} icon={<Camera className="h-5 w-5" />} inactiveIcon={<CameraOff className="h-5 w-5" />} />
        <button
          onClick={() => setShowCoco((v) => !v)}
          className={`flex min-w-20 flex-col items-center gap-1 rounded-xl px-3 py-2 text-xs font-semibold transition-colors ${
            showCoco ? 'bg-violet-600 text-white' : 'bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-700'
          }`}
        >
          <Sparkles className="h-5 w-5" /><span>Coco</span>
        </button>
        <button
          onClick={() => setShowTranscript((v) => !v)}
          className={`flex min-w-20 flex-col items-center gap-1 rounded-xl px-3 py-2 text-xs font-semibold transition-colors ${
            showTranscript ? 'bg-blue-600 text-white' : 'bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 hover:bg-slate-200 dark:hover:bg-slate-700'
          }`}
        >
          <Captions className="h-5 w-5" /><span>Transcript</span>
        </button>
        <ToggleButton label="Share" enabled={isScreenShareEnabled} onClick={() => void toggle('screen')} icon={<MonitorUp className="h-5 w-5" />} inactiveIcon={<MonitorUp className="h-5 w-5" />} />
        <ToggleButton
          label={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}
          enabled
          onClick={() => void onToggleFullscreen().catch((fullscreenError) => setMediaError(fullscreenError instanceof Error ? fullscreenError.message : 'Fullscreen could not be changed.'))}
          icon={isFullscreen ? <Minimize2 className="h-5 w-5" /> : <Maximize2 className="h-5 w-5" />}
          inactiveIcon={isFullscreen ? <Minimize2 className="h-5 w-5" /> : <Maximize2 className="h-5 w-5" />}
        />
        <MeetingRecorder roomName={roomName} onError={setMediaError} />
        <button
          onClick={handleLeaveMeeting}
          disabled={isProcessingPipeline}
          className="flex min-w-20 flex-col items-center gap-1 rounded-xl bg-rose-600 px-3 py-2 text-xs font-semibold text-white hover:bg-rose-700 disabled:cursor-not-allowed disabled:opacity-60 cursor-pointer"
        >
          <LogOut className="h-5 w-5" /><span>Leave</span>
        </button>
      </div>

      {/* Honest leave state: backend finalization is room-wide after everyone disconnects. */}
      {isProcessingPipeline && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 p-4 backdrop-blur-md animate-fade-in font-sans">
          <div className="w-full max-w-md rounded-2xl bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 p-6 shadow-2xl space-y-4">
            <div className="flex items-center space-x-3">
              <div className="p-2.5 bg-blue-50 dark:bg-blue-950/80 rounded-2xl text-blue-600 dark:text-blue-400">
                <Sparkles className="w-6 h-6 animate-pulse" />
              </div>
              <div>
                <h3 className="text-base font-extrabold text-slate-900 dark:text-white font-sans">
                  Leaving Meeting
                </h3>
                <p className="text-xs text-slate-500 dark:text-slate-400">
                  Captured transcript content will be finalized after the room ends.
                </p>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export const MeetingRoomView: React.FC = () => {
  const { currentUser, pendingRoomJoin, setPendingRoomJoin } = useApp();
  const fullscreenRootRef = useRef<HTMLDivElement>(null);
  const [roomName, setRoomName] = useState('');
  const [displayName, setDisplayName] = useState(currentUser.name);
  const [joinDetails, setJoinDetails] = useState<JoinDetails | null>(null);
  const [error, setError] = useState('');
  const [isJoining, setIsJoining] = useState(false);
  const [isFullscreen, setIsFullscreen] = useState(false);

  useEffect(() => {
    const updateFullscreenState = () => setIsFullscreen(document.fullscreenElement === fullscreenRootRef.current);
    document.addEventListener('fullscreenchange', updateFullscreenState);
    return () => document.removeEventListener('fullscreenchange', updateFullscreenState);
  }, []);

  const joinWithRoomName = async (targetRoomName: string, targetDisplayName: string) => {
    setIsJoining(true); setError('');
    try { setJoinDetails(await getJoinDetails(targetRoomName.trim(), targetDisplayName.trim())); }
    catch (joinError) { setError(joinError instanceof Error ? joinError.message : 'Unable to join meeting.'); }
    finally { setIsJoining(false); }
  };

  const joinRoom = async (event: React.FormEvent) => {
    event.preventDefault();
    await joinWithRoomName(roomName, displayName);
  };

  // Arriving here via InvitationCard's "Enter Room" skips the manual entry
  // screen and joins the meeting's own room directly. Cleared immediately
  // so navigating away and back to Live Meeting later falls through to the
  // normal manual-entry screen, not a stale auto-join.
  useEffect(() => {
    if (!pendingRoomJoin) return;
    const { roomName: targetRoom, displayName: targetDisplayName } = pendingRoomJoin;
    setPendingRoomJoin(null);
    setRoomName(targetRoom);
    void joinWithRoomName(targetRoom, targetDisplayName);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingRoomJoin]);

  const toggleFullscreen = async () => {
    if (document.fullscreenElement === fullscreenRootRef.current) {
      await document.exitFullscreen();
      return;
    }
    if (!fullscreenRootRef.current?.requestFullscreen) throw new Error('Fullscreen is not supported by this browser.');
    await fullscreenRootRef.current.requestFullscreen();
  };

  return (
    <div ref={fullscreenRootRef} className={isFullscreen ? 'h-screen w-screen overflow-y-auto bg-slate-50 dark:bg-slate-950' : ''}>
      {joinDetails ? (
        <LiveKitRoom token={joinDetails.token} serverUrl={joinDetails.serverUrl} connect audio video onError={(roomError) => setError(roomError.message)}>
          <RoomAudioRenderer />
          <RoomContent
            roomName={joinDetails.roomName}
            displayName={joinDetails.displayName}
            token={joinDetails.token}
            isFullscreen={isFullscreen}
            onToggleFullscreen={toggleFullscreen}
            onLeave={() => {
              if (document.fullscreenElement === fullscreenRootRef.current) void document.exitFullscreen();
              setJoinDetails(null);
              setError('');
            }}
          />
        </LiveKitRoom>
      ) : (
        <div className="mx-auto flex min-h-[calc(100vh-8rem)] max-w-2xl items-center p-4 sm:p-6">
          <form onSubmit={joinRoom} className="w-full rounded-3xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-6 shadow-xl dark:shadow-2xl dark:shadow-slate-950/50 sm:p-10 transition-all">
            <div className="mb-6 flex h-12 w-12 items-center justify-center rounded-2xl bg-blue-600 dark:bg-blue-600 text-white shadow-md shadow-blue-600/30">
              <Video className="h-6 w-6" />
            </div>
            <h1 className="text-2xl font-extrabold tracking-tight text-slate-900 dark:text-white font-sans">
              Join a Live Meeting Room
            </h1>
            <p className="mt-2 text-sm leading-relaxed text-slate-500 dark:text-slate-400">
              Enter a <strong className="text-slate-700 dark:text-slate-200">Meeting Room ID</strong> to join or create a session.
            </p>

            {error && (
              <div className="mt-5 flex gap-2 rounded-xl border border-rose-200 dark:border-rose-900/60 bg-rose-50 dark:bg-rose-950/40 p-3 text-sm text-rose-700 dark:text-rose-300">
                <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                {error}
              </div>
            )}

            <div className="mt-6 space-y-2">
              <label className="block text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
                Meeting Room ID
              </label>
              <input
                value={roomName}
                onChange={(event) => setRoomName(event.target.value.replace(/[^a-zA-Z0-9_-]/g, ''))}
                required
                maxLength={80}
                className="w-full rounded-xl border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 px-3.5 py-2.5 text-sm text-slate-900 dark:text-white outline-none focus:border-blue-600 dark:focus:border-blue-500 font-mono font-bold placeholder-slate-400 dark:placeholder-slate-500 transition-colors shadow-2xs"
                placeholder="e.g. 1111"
              />
            </div>

            <label className="mt-4 block text-xs font-bold uppercase tracking-wider text-slate-500 dark:text-slate-400">
              Your Display Name
              <input
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                required
                maxLength={80}
                className="mt-2 w-full rounded-xl border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 px-3.5 py-2.5 text-sm text-slate-900 dark:text-white outline-none focus:border-blue-600 dark:focus:border-blue-500 font-semibold transition-colors shadow-2xs"
              />
            </label>

            <button
              disabled={isJoining || !roomName || !displayName}
              className="mt-6 flex w-full items-center justify-center gap-2 rounded-xl bg-blue-600 dark:bg-blue-600 px-4 py-3 text-sm font-bold text-white hover:bg-blue-700 dark:hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer shadow-md shadow-blue-600/20 dark:shadow-blue-900/40 transition-all font-sans"
            >
              {isJoining ? 'Joining…' : 'Join Meeting'}
            </button>
            <button type="button" onClick={() => void toggleFullscreen().catch((fullscreenError) => setError(fullscreenError instanceof Error ? fullscreenError.message : 'Fullscreen could not be changed.'))} className="mt-3 flex w-full items-center justify-center gap-2 rounded-xl border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 px-4 py-3 text-sm font-bold text-slate-700 dark:text-slate-300 hover:bg-slate-50 dark:hover:bg-slate-700 transition-colors"><Maximize2 className="h-5 w-5" /> Fullscreen</button>
          </form>
        </div>
      )}
    </div>
  );
};
