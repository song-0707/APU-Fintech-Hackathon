import React, { useState } from 'react';
import { useApp } from '../context/AppContext';
import { IntelligenceSection, MeetingIntelligenceOverview } from './MeetingIntelligenceOverview';
import { MeetingDetailView } from './MeetingDetailView';
import { Meeting } from '../types';

// meetings (from useApp) is already scoped to currentUser — the backend's
// /meetings enforces that (see backend/app/api/meetings.py) — so this used
// to re-filter it client-side with a looser, fuzzy name match; removed
// rather than risk the two silently disagreeing.
export const MeetingIntelligenceView: React.FC = () => {
  const { meetings, currentUser, openDmWithUser, selectedMeetingId, setSelectedMeetingId } = useApp();

  const [selectedMeeting, setSelectedMeeting] = useState<Meeting | null>(null);
  const [activeSection, setActiveSection] = useState<IntelligenceSection>('meetings');

  const handleSelectMeeting = (meeting: Meeting) => {
    setSelectedMeeting(meeting);
    setSelectedMeetingId(meeting.id);
  };

  const handleBackToOverview = () => {
    setSelectedMeetingId(null);
    setSelectedMeeting(null);
  };

  const handleSendDirectMessage = (recipientName: string, text: string) => {
    openDmWithUser(recipientName);
  };

  return (
    <div className="max-w-[1920px] w-full mx-auto px-8 py-6 animate-fade-in font-sans">
      {selectedMeetingId && (selectedMeeting || meetings.find(m => m.id === selectedMeetingId)) ? (
        <MeetingDetailView
          selectedMeetingId={selectedMeetingId}
          meeting={selectedMeeting || undefined}
          meetings={meetings}
          currentUser={currentUser}
          onBackToDashboard={handleBackToOverview}
          onSendDirectMessage={handleSendDirectMessage}
        />
      ) : (
        <MeetingIntelligenceOverview
          meetings={meetings}
          activeSection={activeSection}
          onSectionChange={setActiveSection}
          onSelectMeeting={handleSelectMeeting}
        />
      )}
    </div>
  );
};

