import React, { useEffect, useMemo, useState } from 'react';
import {
  ArrowRight,
  BrainCircuit,
  Calendar,
  CheckSquare,
  ClipboardList,
  Clock,
  Filter,
  History,
  Layers,
  Sparkles,
} from 'lucide-react';
import { ActionItem, Decision, Meeting } from '../types';
import { MeetingCard } from './MeetingCard';

export type IntelligenceSection = 'meetings' | 'decisions' | 'tasks';

interface MeetingIntelligenceOverviewProps {
  meetings: Meeting[];
  activeSection: IntelligenceSection;
  onSectionChange: (section: IntelligenceSection) => void;
  onSelectMeeting: (meeting: Meeting) => void;
}

interface DecisionTimelineEntry { decision: Decision; meeting: Meeting; }
interface ActionItemEntry { actionItem: ActionItem; meeting: Meeting; }

const impactStyles: Record<NonNullable<Decision['impactLevel']>, string> = {
  High: 'bg-rose-50 text-rose-700 border-rose-200 dark:bg-rose-950/60 dark:text-rose-300 dark:border-rose-800',
  Medium: 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/60 dark:text-amber-300 dark:border-amber-800',
  Low: 'bg-slate-100 text-slate-600 border-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700',
};

const priorityStyles: Record<NonNullable<ActionItem['priority']>, string> = {
  High: 'bg-rose-50 text-rose-700 border-rose-200 dark:bg-rose-950/60 dark:text-rose-300 dark:border-rose-800',
  Medium: 'bg-amber-50 text-amber-700 border-amber-200 dark:bg-amber-950/60 dark:text-amber-300 dark:border-amber-800',
  Low: 'bg-slate-100 text-slate-600 border-slate-200 dark:bg-slate-800 dark:text-slate-300 dark:border-slate-700',
};

const Pill: React.FC<{ children: React.ReactNode; className: string }> = ({ children, className }) => (
  <span className={`rounded-full border px-2 py-0.5 text-[10px] font-bold ${className}`}>{children}</span>
);

const EmptyState: React.FC<{ icon: React.ReactNode; title: string; description: string }> = ({ icon, title, description }) => (
  <div className="flex min-h-48 flex-col items-center justify-center rounded-2xl border border-dashed border-slate-300 bg-slate-50 px-6 text-center dark:border-slate-700 dark:bg-slate-900/50">
    <div className="mb-3 text-slate-400">{icon}</div>
    <h3 className="text-sm font-bold text-slate-800 dark:text-white">{title}</h3>
    <p className="mt-1 max-w-sm text-xs leading-relaxed text-slate-500 dark:text-slate-400">{description}</p>
  </div>
);

export const MeetingIntelligenceOverview: React.FC<MeetingIntelligenceOverviewProps> = ({ meetings, activeSection, onSectionChange, onSelectMeeting }) => {
  const [categoryFilter, setCategoryFilter] = useState('ALL');
  const [meetingsPage, setMeetingsPage] = useState(0);
  const MEETINGS_PAGE_SIZE = 6;

  useEffect(() => {
    setMeetingsPage(0);
  }, [categoryFilter]);

  const completedMeetings = useMemo(() => meetings.filter((meeting) => meeting.status === 'Completed'), [meetings]);
  const visibleMeetings = useMemo(() => meetings.filter((meeting) => (
    meeting.status === 'Completed'
    || meeting.status === 'Pending'
    || meeting.status === 'Preprocessing'
    || meeting.status === 'ASR'
    || meeting.status === 'LLM'
    || meeting.status === 'Graph'
    || meeting.status === 'Retrying'
    || meeting.status === 'Failed'
  )), [meetings]);

  const metrics = useMemo(() => {
    const decisions = completedMeetings.flatMap((meeting) => meeting.decisions);
    const totalActionItems = completedMeetings.reduce((count, meeting) => count + meeting.actionItems.length, 0);
    return {
      completedCount: completedMeetings.length,
      totalDecisions: decisions.length,
      totalActionItems,
      averageConfidence: decisions.length ? Math.round(decisions.reduce((total, decision) => total + decision.confidenceScore, 0) / decisions.length) : 0,
      highImpactDecisions: decisions.filter((decision) => decision.impactLevel === 'High').length,
    };
  }, [completedMeetings]);

  const filteredVisibleMeetings = useMemo(() => visibleMeetings.filter((meeting) => (
    categoryFilter === 'ALL' || meeting.project === categoryFilter || meeting.decisions.some((decision) => decision.category === categoryFilter)
  )), [categoryFilter, visibleMeetings]);

  const pagedVisibleMeetings = useMemo(
    () => filteredVisibleMeetings.slice(meetingsPage * MEETINGS_PAGE_SIZE, (meetingsPage + 1) * MEETINGS_PAGE_SIZE),
    [filteredVisibleMeetings, meetingsPage]
  );
  const totalMeetingsPages = Math.max(1, Math.ceil(filteredVisibleMeetings.length / MEETINGS_PAGE_SIZE));

  const timelineEntries = useMemo<DecisionTimelineEntry[]>(() => completedMeetings
    .flatMap((meeting) => meeting.decisions.map((decision) => ({ decision, meeting })))
    .sort((a, b) => b.meeting.dateTime.localeCompare(a.meeting.dateTime)), [completedMeetings]);

  const actionItemEntries = useMemo<ActionItemEntry[]>(() => completedMeetings
    .flatMap((meeting) => meeting.actionItems.map((actionItem) => ({ actionItem, meeting })))
    .sort((a, b) => b.meeting.dateTime.localeCompare(a.meeting.dateTime)), [completedMeetings]);

  const metricCards: Array<{ section: IntelligenceSection; label: string; value: number; detail: string; icon: React.ReactNode; activeClass: string }> = [
    { section: 'meetings', label: 'Meetings', value: visibleMeetings.length, detail: 'Completed and processing', icon: <Layers className="h-5 w-5" />, activeClass: 'border-blue-500 ring-2 ring-blue-100 dark:ring-blue-950' },
    { section: 'decisions', label: 'Extracted Decisions', value: metrics.totalDecisions, detail: 'Traceable organizational memory', icon: <Sparkles className="h-5 w-5" />, activeClass: 'border-emerald-500 ring-2 ring-emerald-100 dark:ring-emerald-950' },
    { section: 'tasks', label: 'Assigned Tasks', value: metrics.totalActionItems, detail: 'Open source meeting in one click', icon: <CheckSquare className="h-5 w-5" />, activeClass: 'border-amber-500 ring-2 ring-amber-100 dark:ring-amber-950' },
  ];

  return (
    <div className="mx-auto w-full max-w-[1920px] space-y-6 animate-fade-in px-4 py-6 pb-16 font-sans sm:px-8">
      <header className="flex flex-col justify-between gap-4 border-b border-slate-200/80 pb-4 dark:border-slate-800 sm:flex-row sm:items-center">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-extrabold tracking-tight text-slate-900 dark:text-white"><BrainCircuit className="h-6 w-6 text-blue-600 dark:text-blue-400" /> Meeting Intelligence Hub</h1>
          <p className="mt-1 text-xs font-medium text-slate-500 dark:text-slate-400">Cross-meeting decisions, action items, and organizational memory in one place.</p>
        </div>
        <span className="w-fit rounded-full border border-blue-200 bg-blue-50 px-3 py-1 text-xs font-bold text-blue-700 dark:border-blue-800 dark:bg-blue-950/80 dark:text-blue-300">{metrics.completedCount} Indexed Meetings</span>
      </header>

      <section aria-labelledby="quick-overview-title" className="space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-2">
          <div><h2 id="quick-overview-title" className="text-lg font-extrabold tracking-tight text-slate-900 dark:text-white">Quick Overview</h2><p className="mt-1 text-xs font-medium text-slate-500 dark:text-slate-400">Choose a metric to explore the intelligence behind it.</p></div>
          <div className="rounded-lg bg-slate-100 px-2.5 py-1 text-[11px] font-bold text-slate-600 dark:bg-slate-800 dark:text-slate-300">{metrics.averageConfidence}% avg. confidence · {metrics.highImpactDecisions} high-impact</div>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          {metricCards.map((card) => {
            const isActive = card.section === activeSection;
            return <button key={card.section} type="button" onClick={() => onSectionChange(card.section)} aria-pressed={isActive} className={`flex items-center gap-3.5 rounded-2xl border bg-white p-4 text-left shadow-2xs transition-all hover:-translate-y-0.5 hover:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:bg-slate-900 ${isActive ? card.activeClass : 'border-slate-200 dark:border-slate-800'}`}>
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-slate-100 bg-slate-50 text-slate-700 dark:border-slate-800 dark:bg-slate-800 dark:text-slate-200">{card.icon}</div>
              <div className="min-w-0"><div className="font-mono text-2xl font-extrabold tracking-tight text-slate-900 dark:text-white">{card.value}</div><p className="text-[11px] font-bold text-slate-600 dark:text-slate-300">{card.label}</p><p className="mt-0.5 truncate text-[10px] font-medium text-slate-400">{card.detail}</p></div>
            </button>;
          })}
        </div>
      </section>

      {activeSection === 'meetings' && <section className="space-y-4" aria-labelledby="indexed-meetings-title">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div><h2 id="indexed-meetings-title" className="text-lg font-extrabold tracking-tight text-slate-900 dark:text-white">Meetings</h2><p className="mt-1 text-xs font-medium text-slate-500 dark:text-slate-400">Scheduled live meetings appear here while their transcript, decisions, and action items are processing.</p></div>
          <label className="flex items-center gap-2 text-xs font-semibold text-slate-500 dark:text-slate-400"><Filter className="h-4 w-4" /><span className="sr-only">Filter indexed meetings by category</span><select value={categoryFilter} onChange={(event) => setCategoryFilter(event.target.value)} className="cursor-pointer rounded-xl border border-slate-300 bg-white px-3.5 py-2 text-xs font-semibold text-slate-700 shadow-2xs focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200"><option value="ALL">All Categories</option><option value="Core Infrastructure">Core Infrastructure</option><option value="Security & Compliance">Security & Compliance</option><option value="Core Engine v2">Core Engine v2</option></select></label>
        </div>
        {filteredVisibleMeetings.length ? (
          <>
            <div className="grid grid-cols-1 gap-5 md:grid-cols-2 xl:grid-cols-3">
              {pagedVisibleMeetings.map((meeting) => <MeetingCard key={meeting.id} meeting={meeting} onViewDetails={onSelectMeeting} />)}
            </div>
            <div className="flex items-center justify-center gap-4 pt-2">
              <button
                type="button"
                onClick={() => setMeetingsPage((p) => Math.max(0, p - 1))}
                disabled={meetingsPage === 0}
                className="rounded-xl border border-slate-300 bg-white px-3.5 py-2 text-xs font-bold text-slate-700 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200"
              >
                &lt;
              </button>
              <span className="text-xs font-semibold text-slate-500 dark:text-slate-400">Page {meetingsPage + 1} of {totalMeetingsPages}</span>
              <button
                type="button"
                onClick={() => setMeetingsPage((p) => Math.min(totalMeetingsPages - 1, p + 1))}
                disabled={meetingsPage >= totalMeetingsPages - 1}
                className="rounded-xl border border-slate-300 bg-white px-3.5 py-2 text-xs font-bold text-slate-700 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200"
              >
                &gt;
              </button>
            </div>
          </>
        ) : <EmptyState icon={<Layers className="h-6 w-6" />} title="No meetings processing" description="Live meetings will appear here after everyone leaves the room and the AI pipeline starts." />}
      </section>}

      {activeSection === 'decisions' && <section className="space-y-4" aria-labelledby="decision-timeline-title">
        <div className="flex items-end justify-between gap-3"><div><h2 id="decision-timeline-title" className="flex items-center gap-2 text-lg font-extrabold tracking-tight text-slate-900 dark:text-white"><History className="h-5 w-5 text-emerald-600 dark:text-emerald-400" /> Decision Timeline</h2><p className="mt-1 text-xs font-medium text-slate-500 dark:text-slate-400">Most recent decisions first. Select one to trace it back to its meeting.</p></div><span className="shrink-0 text-xs font-medium text-slate-400">{timelineEntries.length} decisions</span></div>
        {timelineEntries.length ? <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-2xs dark:border-slate-800 dark:bg-slate-900 sm:p-6"><ol className="relative ml-2 space-y-4 border-l-2 border-slate-100 pl-5 dark:border-slate-800">{timelineEntries.map(({ decision, meeting }) => <li key={`${meeting.id}-${decision.id}`} className="relative"><span className="absolute -left-[1.72rem] top-5 h-3 w-3 rounded-full border-2 border-white bg-emerald-500 dark:border-slate-900" /><button type="button" onClick={() => onSelectMeeting(meeting)} className="group w-full rounded-xl border border-slate-200 bg-slate-50 p-4 text-left transition-all hover:border-emerald-400 hover:bg-white hover:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 dark:border-slate-800 dark:bg-slate-950/40 dark:hover:border-emerald-500 dark:hover:bg-slate-950" aria-label={`Open source meeting for decision: ${decision.title}`}><div className="flex flex-wrap items-center gap-2"><span className="font-mono text-[11px] font-bold text-slate-400">{meeting.dateTime}</span>{decision.impactLevel && <Pill className={impactStyles[decision.impactLevel]}>{decision.impactLevel} impact</Pill>}{decision.category && <Pill className="border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-800 dark:bg-blue-950/60 dark:text-blue-300">{decision.category}</Pill>}</div><div className="mt-2 flex items-start justify-between gap-3"><div><h3 className="text-sm font-bold text-slate-900 transition-colors group-hover:text-emerald-700 dark:text-white dark:group-hover:text-emerald-300">{decision.title}</h3>{decision.rationale && <p className="mt-1.5 line-clamp-2 text-xs leading-relaxed text-slate-600 dark:text-slate-300">{decision.rationale}</p>}</div><ArrowRight className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600 transition-transform group-hover:translate-x-1 dark:text-emerald-400" /></div><div className="mt-3 flex flex-wrap items-center gap-3 border-t border-slate-200 pt-3 text-[11px] font-semibold text-slate-500 dark:border-slate-800 dark:text-slate-400"><span className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400"><Calendar className="h-3.5 w-3.5" />{meeting.title}</span><span>{decision.confidenceScore}% confidence</span></div></button></li>)}</ol></div> : <EmptyState icon={<History className="h-6 w-6" />} title="No extracted decisions" description="Decisions will appear here after completed meetings are analyzed." />}
      </section>}

      {activeSection === 'tasks' && <section className="space-y-4" aria-labelledby="action-items-title">
        <div className="flex items-end justify-between gap-3"><div><h2 id="action-items-title" className="flex items-center gap-2 text-lg font-extrabold tracking-tight text-slate-900 dark:text-white"><ClipboardList className="h-5 w-5 text-amber-600 dark:text-amber-400" /> Assigned Tasks</h2><p className="mt-1 text-xs font-medium text-slate-500 dark:text-slate-400">Every task stays connected to the meeting where it was assigned.</p></div><span className="shrink-0 text-xs font-medium text-slate-400">{actionItemEntries.length} tasks</span></div>
        {actionItemEntries.length ? <div className="rounded-2xl border border-slate-200 bg-white p-4 shadow-2xs dark:border-slate-800 dark:bg-slate-900 sm:p-6"><div className="grid grid-cols-1 gap-3 lg:grid-cols-2">{actionItemEntries.map(({ actionItem, meeting }) => <button key={`${meeting.id}-${actionItem.id}`} type="button" onClick={() => onSelectMeeting(meeting)} className="group rounded-xl border border-slate-200 bg-slate-50 p-4 text-left transition-all hover:border-amber-400 hover:bg-white hover:shadow-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-500 dark:border-slate-800 dark:bg-slate-950/40 dark:hover:border-amber-500 dark:hover:bg-slate-950" aria-label={`Open source meeting for task: ${actionItem.task}`}><div className="flex items-start justify-between gap-3"><h3 className="line-clamp-2 text-sm font-bold text-slate-900 transition-colors group-hover:text-amber-700 dark:text-white dark:group-hover:text-amber-300">{actionItem.task}</h3><ArrowRight className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 transition-transform group-hover:translate-x-1 dark:text-amber-400" /></div><div className="mt-3 flex flex-wrap gap-2"><Pill className="border-slate-200 bg-white text-slate-600 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-300">{actionItem.assignee}</Pill><Pill className={priorityStyles[actionItem.priority ?? 'Low']}>{actionItem.priority ?? 'No priority'}</Pill><Pill className="border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-800 dark:bg-blue-950/60 dark:text-blue-300">{actionItem.status}</Pill></div><div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-slate-200 pt-3 text-[11px] font-semibold text-slate-500 dark:border-slate-800 dark:text-slate-400"><span className="inline-flex items-center gap-1"><Clock className="h-3.5 w-3.5" />Due {actionItem.dueDate}</span><span className="inline-flex items-center gap-1 text-blue-600 dark:text-blue-400"><Calendar className="h-3.5 w-3.5" />{meeting.title}</span></div></button>)}</div></div> : <EmptyState icon={<CheckSquare className="h-6 w-6" />} title="No assigned tasks" description="Action items from completed meetings will appear here." />}
      </section>}
    </div>
  );
};
