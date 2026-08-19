import { AlertTriangle, Captions, ListChecks, Sparkles } from 'lucide-react';
import React from 'react';
import type { CaptionLine, LiveMinuteSummary } from '../hooks/useLiveMeetingSession';

export const LiveTranscriptPanel: React.FC<{
  transcript: CaptionLine[];
  minuteSummaries: LiveMinuteSummary[];
  error: string;
}> = ({ transcript, minuteSummaries, error }) => {
  const sortedTranscript = [...transcript].sort((a, b) => (a.start ?? 0) - (b.start ?? 0));

  return (
    <section className="rounded-2xl border border-slate-200 bg-white p-4 space-y-4">
      <div className="flex items-center gap-2 text-sm font-bold text-slate-700">
        <Captions className="h-4 w-4 text-blue-600" /> Live transcript
      </div>

      {error && <div className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">{error}</div>}

      {minuteSummaries.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-xs font-bold uppercase text-slate-400">
            <Sparkles className="h-3.5 w-3.5 text-violet-500" /> Provisional minute intelligence
          </div>
          <div className="grid gap-2 lg:grid-cols-2">
            {minuteSummaries.map((summary) => (
              <article key={summary.id} className="rounded-xl border border-violet-100 bg-violet-50/40 p-3 text-xs text-slate-700">
                <div className="mb-1 flex items-center justify-between gap-2">
                  <span className="font-bold text-violet-700">{summary.label}</span>
                  <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[10px] font-semibold text-violet-700">Live</span>
                </div>
                <p>{summary.summary}</p>
                {summary.decisions.length > 0 && (
                  <div className="mt-2">
                    <div className="font-bold text-slate-600">Decisions</div>
                    <ul className="mt-1 list-disc space-y-0.5 pl-4">
                      {summary.decisions.map((decision, idx) => <li key={idx}>{decision}</li>)}
                    </ul>
                  </div>
                )}
                {summary.action_items.length > 0 && (
                  <div className="mt-2">
                    <div className="flex items-center gap-1 font-bold text-slate-600">
                      <ListChecks className="h-3.5 w-3.5 text-blue-600" /> Action items
                    </div>
                    <ul className="mt-1 list-disc space-y-0.5 pl-4">
                      {summary.action_items.map((item, idx) => (
                        <li key={idx}>{item.task}{item.assignee ? ` — ${item.assignee}` : ''}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {summary.risks.length > 0 && (
                  <div className="mt-2">
                    <div className="flex items-center gap-1 font-bold text-slate-600">
                      <AlertTriangle className="h-3.5 w-3.5 text-amber-600" /> Risks
                    </div>
                    <ul className="mt-1 list-disc space-y-0.5 pl-4">
                      {summary.risks.map((risk, idx) => <li key={idx}>{risk}</li>)}
                    </ul>
                  </div>
                )}
              </article>
            ))}
          </div>
        </div>
      )}

      <div className="max-h-64 overflow-y-auto space-y-2 pr-1">
        {sortedTranscript.length === 0 && !error && (
          <p className="text-xs text-slate-400">Captions will appear here once someone turns on Live Transcript.</p>
        )}
        {sortedTranscript.map((line) => (
          <div key={line.id} className="text-xs text-slate-700">
            <span className="font-bold text-blue-700">{line.speaker}</span>
            <span className="ml-1.5 text-slate-400">{line.start !== undefined ? `${Math.floor(line.start / 60).toString().padStart(2, '0')}:${Math.floor(line.start % 60).toString().padStart(2, '0')}` : line.timestamp}</span>
            <p className="mt-0.5">{line.text}</p>
          </div>
        ))}
      </div>
    </section>
  );
};
