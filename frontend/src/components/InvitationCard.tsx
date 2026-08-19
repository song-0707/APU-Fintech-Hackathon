import React from 'react';
import { Meeting } from '../types';

interface InvitationCardProps {
  meeting: Meeting;
  onEnterRoom: (meeting: Meeting) => void;
  onReject: (meetingId: string) => void;
}

export const InvitationCard: React.FC<InvitationCardProps> = ({ meeting, onEnterRoom, onReject }) => {
  return (
    <div className="bg-white dark:bg-slate-900 border border-slate-200/80 dark:border-slate-800 rounded-2xl p-4 shadow-2xs space-y-3">
      <div className="space-y-1">
        <h4 className="text-sm font-bold text-slate-900 dark:text-white">{meeting.title}</h4>
        <p className="text-xs text-slate-500 dark:text-slate-400">{meeting.participants.join(', ')}</p>
        <p className="text-xs text-slate-400">{meeting.dateTime}</p>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <button
          type="button"
          onClick={() => onEnterRoom(meeting)}
          className="py-2.5 px-3 rounded-xl bg-blue-600 hover:bg-blue-700 text-white font-semibold text-xs transition-all cursor-pointer"
        >
          Enter Room
        </button>
        <button
          type="button"
          onClick={() => onReject(meeting.id)}
          className="py-2.5 px-3 rounded-xl bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-700 text-slate-700 dark:text-slate-300 font-semibold text-xs transition-all cursor-pointer"
        >
          Reject
        </button>
      </div>
    </div>
  );
};
