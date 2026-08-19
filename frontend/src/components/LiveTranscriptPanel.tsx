import { Captions } from 'lucide-react';
import React from 'react';
import type { CaptionLine } from '../hooks/useLiveMeetingSession';

export const LiveTranscriptPanel: React.FC<{ transcript: CaptionLine[]; error: string }> = ({ transcript, error }) => (
  <section className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-4 space-y-3">
    <div className="flex items-center gap-2 text-sm font-bold text-slate-700 dark:text-slate-300">
      <Captions className="h-4 w-4 text-blue-600 dark:text-blue-400" /> Live transcript
    </div>

    {error && <div className="rounded-xl border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 p-3 text-xs text-amber-800 dark:text-amber-300">{error}</div>}

    <div className="max-h-64 overflow-y-auto space-y-2 pr-1">
      {transcript.length === 0 && !error && (
        <p className="text-xs text-slate-400">Captions will appear here once someone turns on Live Transcript.</p>
      )}
      {transcript.map((line) => (
        <div key={line.id} className="text-xs text-slate-700 dark:text-slate-300">
          <span className="font-bold text-blue-700 dark:text-blue-400">{line.speaker}</span>
          <span className="ml-1.5 text-slate-400">{line.timestamp}</span>
          <p className="mt-0.5">{line.text}</p>
        </div>
      ))}
    </div>
  </section>
);
