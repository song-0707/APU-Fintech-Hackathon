import React, { useState, useMemo, useRef } from 'react';
import { useApp } from '../context/AppContext';
import * as api from '../services/api';
import { InvitationCard } from './InvitationCard';
import { Meeting } from '../types';
import {
  Clock,
  CheckCircle2,
  Search,
  Filter,
  ArrowRight,
  FileText,
  X,
  ClipboardList
} from 'lucide-react';

export const DashboardView: React.FC = () => {
  const {
    meetings,
    currentUser,
    setActiveTab,
    setSelectedMeetingId,
    rsvpToMeeting,
    setPendingRoomJoin
  } = useApp();

  const [searchQuery, setSearchQuery] = useState('');
  const [projectFilter, setProjectFilter] = useState('ALL');
  const [dashboardSection, setDashboardSection] = useState<'upcoming' | 'tasks'>('upcoming');
  const upcomingSectionRef = useRef<HTMLDivElement>(null);

  // meetings is already scoped to currentUser — the backend's /meetings
  // enforces that (see backend/app/api/meetings.py), so re-filtering it
  // client-side would just risk silently disagreeing with the server.
  //
  // Filtered lists for upcoming and completed
  const completedMeetings = useMemo(() => {
    return meetings.filter(m => m.status === 'Completed');
  }, [meetings]);

  const filteredCompleted = useMemo(() => {
    return completedMeetings.filter(m => {
      const matchesProject = projectFilter === 'ALL' || m.project === projectFilter;
      const matchesSearch = !searchQuery.trim() ||
        m.title.toLowerCase().includes(searchQuery.toLowerCase()) ||
        m.project.toLowerCase().includes(searchQuery.toLowerCase()) ||
        (m.participants && m.participants.some(p => (typeof p === 'string' ? p : (p as any).name).toLowerCase().includes(searchQuery.toLowerCase())));
      return matchesProject && matchesSearch;
    });
  }, [completedMeetings, projectFilter, searchQuery]);

  const invitationMeetings = useMemo(
    () => meetings.filter(m => m.source === 'scheduled' && m.status === 'Scheduled'),
    [meetings]
  );

  const myTasks = useMemo(() => {
    return meetings.flatMap(m =>
      (m.actionItems || [])
        .filter(item => item.assignee.toLowerCase() === currentUser.name.toLowerCase())
        .map(item => ({ item, meeting: m }))
    );
  }, [meetings, currentUser.name]);

  const handleEnterRoom = async (meeting: Meeting) => {
    if (!meeting.roomId) return;
    await rsvpToMeeting(meeting.id, 'accepted');
    setPendingRoomJoin({ roomName: meeting.roomId, displayName: currentUser.name });
    setActiveTab('live-meeting');
  };

  const handleReject = (meetingId: string) => {
    void rsvpToMeeting(meetingId, 'declined');
  };

  const scrollToUpcoming = () => {
    setDashboardSection('upcoming');
    upcomingSectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <div className="max-w-[1920px] w-full mx-auto px-8 py-6 space-y-6 font-sans animate-fade-in pb-16">
      
      {/* Hero Welcome Card */}
      <div className="rounded-3xl bg-gradient-to-br from-blue-600 to-blue-700 dark:from-blue-700 dark:to-blue-900 p-6 sm:p-8 shadow-lg text-white space-y-6">
        <div>
          <h1 className="text-2xl sm:text-3xl font-extrabold tracking-tight font-sans">
            Welcome back, {currentUser.name.split(' ')[0]}
          </h1>
          <p className="text-sm text-blue-100 mt-1">
            Personalized dashboard showing scheduled meetings and indexed decision records for your account.
          </p>
        </div>

        <div className="grid grid-cols-3 gap-4">
          <button
            type="button"
            onClick={scrollToUpcoming}
            className="rounded-2xl bg-white/10 hover:bg-white/20 transition-colors p-4 text-left cursor-pointer"
          >
            <div className="text-3xl sm:text-4xl font-extrabold font-mono">{invitationMeetings.length}</div>
            <div className="text-xs sm:text-sm font-semibold text-blue-100 mt-1">Upcoming</div>
          </button>
          <button
            type="button"
            onClick={() => setActiveTab('meetings')}
            className="rounded-2xl bg-white/10 hover:bg-white/20 transition-colors p-4 text-left cursor-pointer"
          >
            <div className="text-3xl sm:text-4xl font-extrabold font-mono">{completedMeetings.length}</div>
            <div className="text-xs sm:text-sm font-semibold text-blue-100 mt-1">Completed</div>
          </button>
          <button
            type="button"
            onClick={() => setDashboardSection(prev => prev === 'tasks' ? 'upcoming' : 'tasks')}
            className="rounded-2xl bg-white/10 hover:bg-white/20 transition-colors p-4 text-left cursor-pointer"
          >
            <div className="text-3xl sm:text-4xl font-extrabold font-mono">{myTasks.length}</div>
            <div className="text-xs sm:text-sm font-semibold text-blue-100 mt-1">Tasks</div>
          </button>
        </div>
      </div>

      {/* Streamlined Search & Filter Bar */}
      <div className="flex flex-col sm:flex-row items-center justify-between gap-3 bg-slate-50/50 dark:bg-slate-900/50 p-2 rounded-2xl border border-slate-200/80 dark:border-slate-800">
        <div className="relative w-full sm:w-96">
          <Search className="w-4 h-4 text-slate-400 absolute left-3.5 top-2.5" />
          <input
            type="text"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search title, participant, or project..."
            className="w-full pl-10 pr-4 py-2 text-xs bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl text-slate-900 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500 transition-colors"
          />
        </div>

        <div className="flex items-center space-x-2 w-full sm:w-auto justify-end">
          <Filter className="w-4 h-4 text-slate-400" />
          <select
            value={projectFilter}
            onChange={(e) => setProjectFilter(e.target.value)}
            className="px-3.5 py-2 text-xs bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-xl text-slate-700 dark:text-slate-300 font-semibold focus:outline-none focus:ring-2 focus:ring-blue-500 cursor-pointer"
          >
            <option value="ALL">All Projects ({meetings.length})</option>
            <option value="Design Systems">Design Systems</option>
            <option value="Core Infrastructure">Core Infrastructure</option>
            <option value="Security & Compliance">Security & Compliance</option>
            <option value="Core Engine v2">Core Engine v2</option>
            <option value="Enterprise Core Platform">Enterprise Core Platform</option>
            <option value="Coco AI Intelligence">Coco AI Intelligence</option>
          </select>
        </div>
      </div>

      {/* Section 1: Upcoming Meetings OR My Tasks */}
      <div ref={upcomingSectionRef} className="space-y-3 scroll-mt-6">
        {dashboardSection === 'upcoming' ? (
          <>
            <div className="flex items-center justify-between">
              <h3 className="text-base font-extrabold text-slate-900 dark:text-white font-sans flex items-center space-x-2">
                <Clock className="w-4 h-4 text-amber-500" />
                <span>Upcoming Meetings</span>
              </h3>
              <span className="text-xs text-slate-400 font-semibold">{invitationMeetings.length} items</span>
            </div>

            {invitationMeetings.length === 0 ? (
              <div className="p-6 text-center bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl space-y-1">
                <p className="text-xs font-semibold text-slate-500 dark:text-slate-400">
                  No upcoming meetings scheduled for {currentUser.name}.
                </p>
              </div>
            ) : (
              <div className="space-y-3">
                {invitationMeetings.map((mtg) => (
                  <InvitationCard key={mtg.id} meeting={mtg} onEnterRoom={handleEnterRoom} onReject={handleReject} />
                ))}
              </div>
            )}
          </>
        ) : (
          <>
            <div className="flex items-center justify-between">
              <h3 className="text-base font-extrabold text-slate-900 dark:text-white font-sans flex items-center space-x-2">
                <ClipboardList className="w-4 h-4 text-amber-500" />
                <span>My Tasks</span>
              </h3>
              <button
                onClick={() => setDashboardSection('upcoming')}
                className="text-xs font-bold text-blue-600 dark:text-blue-400 hover:underline cursor-pointer"
              >
                Back to Upcoming Meetings
              </button>
            </div>

            {myTasks.length === 0 ? (
              <div className="p-6 text-center bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl space-y-1">
                <p className="text-xs font-semibold text-slate-500 dark:text-slate-400">
                  No tasks assigned to {currentUser.name}.
                </p>
              </div>
            ) : (
              <div className="space-y-2">
                {myTasks.map(({ item, meeting }) => (
                  <div
                    key={item.id}
                    onClick={() => { setSelectedMeetingId(meeting.id); setActiveTab('meetings'); }}
                    className="bg-white dark:bg-slate-900 border border-slate-200/80 dark:border-slate-800 rounded-2xl p-4 shadow-2xs hover:border-blue-300 dark:hover:border-blue-700 transition-all cursor-pointer flex items-center justify-between gap-4"
                  >
                    <div className="min-w-0">
                      <p className="text-sm font-bold text-slate-900 dark:text-white truncate">{item.task}</p>
                      <p className="text-xs text-slate-400 mt-0.5">From: {meeting.title} • Due {item.dueDate}</p>
                    </div>
                    <ArrowRight className="w-3.5 h-3.5 text-blue-600 dark:text-blue-400 shrink-0" />
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {/* Section 2: Completed Meetings */}
      <div className="space-y-3 pt-2">
        <div className="flex items-center justify-between">
          <h3 className="text-base font-extrabold text-slate-900 dark:text-white font-sans flex items-center space-x-2">
            <CheckCircle2 className="w-4 h-4 text-emerald-500" />
            <span>Completed & Indexed Meetings</span>
          </h3>

          <button
            onClick={() => setActiveTab('meetings')}
            className="text-xs font-bold text-blue-600 dark:text-blue-400 hover:underline flex items-center space-x-1 cursor-pointer"
          >
            <span>View All Cards</span>
            <ArrowRight className="w-3.5 h-3.5" />
          </button>
        </div>

        {filteredCompleted.length === 0 ? (
          <div className="p-6 text-center bg-white dark:bg-slate-900 border border-slate-200 dark:border-slate-800 rounded-2xl space-y-1">
            <p className="text-xs font-semibold text-slate-500 dark:text-slate-400">
              No completed meetings recorded for {currentUser.name}.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {filteredCompleted.map((mtg) => (
              <div
                key={mtg.id}
                onClick={() => {
                  setSelectedMeetingId(mtg.id);
                  setActiveTab('meetings');
                }}
                className="bg-white dark:bg-slate-900 border border-slate-200/80 dark:border-slate-800 rounded-2xl p-4 shadow-2xs hover:shadow-sm hover:border-blue-500 transition-all cursor-pointer space-y-3 flex flex-col justify-between group"
              >
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="px-2 py-0.5 rounded-md bg-slate-100 dark:bg-slate-800 text-slate-700 dark:text-slate-300 text-[11px] font-bold">
                      {mtg.project}
                    </span>
                    <span className="text-[11px] font-bold text-emerald-600 dark:text-emerald-400">
                      Completed
                    </span>
                  </div>

                  <h4 className="text-sm font-bold text-slate-900 dark:text-white group-hover:text-blue-600 transition-colors line-clamp-1">
                    {mtg.title}
                  </h4>

                  <div className="text-xs text-slate-400 font-medium">
                    {mtg.dateTime}
                  </div>
                </div>

                <div className="pt-2 border-t border-slate-100 dark:border-slate-800 flex items-center justify-between text-xs text-blue-600 dark:text-blue-400 font-semibold">
                  <span>{mtg.decisions?.length || 0} Decisions • {mtg.actionItems?.length || 0} Tasks</span>
                  <ArrowRight className="w-3.5 h-3.5 group-hover:translate-x-1 transition-transform" />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

    </div>
  );
};
