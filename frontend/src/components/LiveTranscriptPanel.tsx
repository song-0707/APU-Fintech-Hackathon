import React from 'react';
import type { CaptionLine, LiveMinuteSummary } from '../hooks/useLiveMeetingSession';

export const LiveTranscriptPanel: React.FC<{
  transcript: CaptionLine[];
  minuteSummaries: LiveMinuteSummary[];
  error: string;
}> = ({ transcript, minuteSummaries, error }) => (
  <section className="rounded-2xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 p-4 space-y-4">
    <div className="text-sm font-bold text-slate-700 dark:text-slate-300">Live transcript</div>

    {error && <div className="rounded-xl border border-amber-200 dark:border-amber-900/60 bg-amber-50 dark:bg-amber-950/40 p-3 text-xs text-amber-800 dark:text-amber-300">{error}</div>}

    <div className="max-h-64 overflow-y-auto space-y-2 pr-1">
      {transcript.length === 0 && !error && (
        <p className="text-xs text-slate-400">Waiting for live speech...</p>
      )}
      {transcript.map((line) => (
        <div key={line.id} className="text-xs text-slate-700 dark:text-slate-300">
          <span className="font-bold text-blue-700 dark:text-blue-400">{line.speaker}</span>
          <span className="ml-1.5 text-slate-400">{line.timestamp}</span>
          <p className="mt-0.5">{line.text}</p>
        </div>
      ))}
    </div>

    <div className="space-y-2 border-t border-slate-200 pt-3 dark:border-slate-800">
      <div className="text-sm font-bold text-slate-700 dark:text-slate-300">Per-minute intelligence</div>
      {minuteSummaries.length === 0 && (
        <p className="text-xs text-slate-400">Summaries appear after each completed minute.</p>
      )}
      {minuteSummaries.map((item) => (
        <article key={item.id} className="rounded-xl border border-blue-100 bg-blue-50/60 p-3 text-xs text-slate-700 dark:border-blue-900/50 dark:bg-blue-950/30 dark:text-slate-300">
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <h3 className="font-bold text-blue-800 dark:text-blue-300">{item.label}</h3>
            {item.provisional && <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-bold uppercase text-blue-700 dark:bg-blue-900/60 dark:text-blue-300">Provisional</span>}
          </div>
          <p>{item.summary}</p>
          {item.decisions.length > 0 && (
            <div className="mt-2">
              <p className="font-bold text-slate-800 dark:text-slate-200">Decisions</p>
              <ul className="mt-1 list-disc space-y-1 pl-4">
                {item.decisions.map((decision, index) => <li key={index}>{decision}</li>)}
              </ul>
            </div>
          )}
          {item.action_items.length > 0 && (
            <div className="mt-2">
              <p className="font-bold text-slate-800 dark:text-slate-200">Action items</p>
              <ul className="mt-1 list-disc space-y-1 pl-4">
                {item.action_items.map((action, index) => (
                  <li key={index}>
                    {action.task}{action.assignee ? ` - ${action.assignee}` : ''}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {item.risks.length > 0 && (
            <div className="mt-2">
              <p className="font-bold text-slate-800 dark:text-slate-200">Risks</p>
              <ul className="mt-1 list-disc space-y-1 pl-4">
                {item.risks.map((risk, index) => <li key={index}>{risk}</li>)}
              </ul>
            </div>
          )}
        </article>
      ))}
    </div>
  </section>
);
