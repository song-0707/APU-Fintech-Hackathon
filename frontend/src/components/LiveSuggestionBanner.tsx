import { ShieldAlert, X } from 'lucide-react';
import React from 'react';
import type { LiveSuggestion } from '../hooks/useLiveMeetingSession';

export const LiveSuggestionBanner: React.FC<{ suggestions: LiveSuggestion[]; onDismiss: (id: string) => void }> = ({ suggestions, onDismiss }) => {
  if (suggestions.length === 0) return null;

  return (
    <div className="space-y-2">
      {suggestions.map((suggestion) => (
        <div key={suggestion.id} className="flex items-start gap-2 rounded-2xl border border-rose-200 dark:border-rose-900/60 bg-rose-50 dark:bg-rose-950/40 p-3 text-sm text-rose-800 dark:text-rose-300">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-rose-600 dark:text-rose-400" />
          <div className="flex-1">
            <div className="flex items-center gap-2">
              <span className="font-bold">Coco noticed a possible contradiction</span>
              {suggestion.judge === 'keyword_fallback' && (
                <span className="rounded-full bg-rose-200 dark:bg-rose-900/60 px-2 py-0.5 text-[10px] font-semibold text-rose-800 dark:text-rose-300">
                  Pattern-matched — not AI-verified
                </span>
              )}
            </div>
            <p className="mt-1">{suggestion.message}</p>
            {suggestion.contradictsDecisionText && (
              <p className="mt-1 text-xs text-rose-600 dark:text-rose-400">Conflicts with: "{suggestion.contradictsDecisionText}"</p>
            )}
          </div>
          <button onClick={() => onDismiss(suggestion.id)} className="text-rose-400 dark:text-rose-500 hover:text-rose-700 dark:hover:text-rose-300">
            <X className="h-4 w-4" />
          </button>
        </div>
      ))}
    </div>
  );
};
