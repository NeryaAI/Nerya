"use client";
import { useEffect, useState } from 'react';
import { parseStrategyDetailId } from '../../lib/strategyDetail';
import { getWorkspaceIdentity, useWorkspaceIdentity, workspaceGeneration, workspaceStorageKey } from '../../lib/workspaceIdentity';
export type TaskDockTab = 'deliverables' | 'reports' | 'browser' | 'agents' | 'strategy' | `strategy:${string}` | 'files' | 'terminal' | `backtest:${string}` | 'charts' | `result:${string}` | `resource:${string}` | `file:${string}` | `snapshot:${string}` | `instrument:${string}`;
type Preference = { open: boolean; expanded: boolean; selected: string; tabs: TaskDockTab[]; dismissed: TaskDockTab[] };
const KEY = 'nerya.chat.task-dock.v3:';
const valid = (id: unknown): id is TaskDockTab => typeof id === 'string' && (['deliverables', 'reports', 'browser', 'agents', 'strategy', 'files', 'terminal', 'charts'].includes(id) || ['backtest:', 'result:', 'resource:', 'file:', 'snapshot:', 'instrument:'].some(prefix => id.startsWith(prefix)) || parseStrategyDetailId(id) !== null);
export function loadTaskDockPreferences(workspace: string | null): Record<string, Preference> {
  if (!workspace || workspace !== getWorkspaceIdentity()) return {};
  try {
    const data = JSON.parse(sessionStorage.getItem(workspaceStorageKey(KEY, workspace)) || '{}');
    return Object.fromEntries(Object.entries(data).filter(([, value]) => {
      const p = value as Preference;
      return p && typeof p.open === 'boolean' && typeof p.expanded === 'boolean' && typeof p.selected === 'string'
        && Array.isArray(p.tabs) && p.tabs.every(id => typeof id === 'string') && Array.isArray(p.dismissed) && p.dismissed.every(id => typeof id === 'string');
    }).slice(-24).map(([key, value]) => { const p = value as Preference; return [key, { ...p, selected: p.selected === 'canvas' ? '' : p.selected, tabs: p.tabs.filter(valid), dismissed: p.dismissed.filter(valid) }]; })) as Record<string, Preference>;
  } catch { return {}; }
}
/** Per-conversation UI state. Closing tabs never cancels runtime work. */
export function useTaskDock(session: string, available: TaskDockTab[], automatic: TaskDockTab[] = available, conversationFirst = false) {
  const workspace = useWorkspaceIdentity();
  const generation = workspaceGeneration();
  const [preferences, setPreferences] = useState<Record<string, Preference>>({});
  const [loadedWorkspace, setLoadedWorkspace] = useState<string | null>(null);
  const [loadedGeneration, setLoadedGeneration] = useState(-1);
  const ready = Boolean(workspace && loadedWorkspace === workspace && loadedGeneration === generation);
  // Conversation-first surfaces enter the transcript, including old full-screen preferences.
  // Only a deliberate workspace action may cover it; resource discovery is not navigation.
  const [openedSession, setOpenedSession] = useState('');
  useEffect(() => setOpenedSession(''), [session, workspace, generation]);
  useEffect(() => { setPreferences(loadTaskDockPreferences(workspace)); setLoadedWorkspace(workspace); setLoadedGeneration(generation); }, [workspace, generation]);
  const saved = ready ? preferences[session] : undefined;
  const tabs = (ready ? [...new Set([...(saved?.tabs || []), ...automatic.filter(id => !saved?.dismissed.includes(id))])] : [])
    .filter(id => ['files', 'browser', 'terminal'].includes(id) || id.startsWith('file:') || parseStrategyDetailId(id) !== null || available.includes(id));
  const selected = saved && tabs.includes(saved.selected as TaskDockTab) ? saved.selected : tabs[0] || '';
  // If all previously saved tabs disappeared, do not leave a blank panel covering chat.
  // Explicitly opened empty workspaces (saved.tabs=[]), files and browser tabs still work.
  const orphaned = Boolean(saved?.tabs.length && !tabs.length);
  const open = ready && !!session && !orphaned && (!conversationFirst || openedSession === session) && (saved?.open ?? tabs.length > 0);
  const expanded = open && (saved?.expanded ?? false);
  const current: Preference = { open, expanded, selected, tabs, dismissed: saved?.dismissed || [] };
  useEffect(() => {
    if (!ready || saved || !tabs.length) return;
    setPreferences(old => old[session] ? old : { ...old, [session]: { open: true, expanded: false, selected, tabs, dismissed: [] } });
  }, [ready, saved, session, selected, tabs]);
  useEffect(() => {
    if (!ready || !workspace || workspace !== getWorkspaceIdentity()) return;
    try { sessionStorage.setItem(workspaceStorageKey(KEY, workspace), JSON.stringify(Object.fromEntries(Object.entries(preferences).slice(-24)))); }
    catch { /* Optional UI cache. */ }
  }, [preferences, ready, workspace]);
  function change(patch: Partial<Preference>) {
    if (!ready || workspace !== getWorkspaceIdentity() || generation !== workspaceGeneration()) return;
    setOpenedSession(session);
    setPreferences(old => ({ ...old, [session]: { ...current, ...patch } }));
  }
  return { open, expanded, selected, tabs,
    select: (tab: TaskDockTab) => change({ open: true, selected: tab, tabs: [...new Set([...tabs, tab])], dismissed: current.dismissed.filter(id => id !== tab) }),
    remove: (tab: TaskDockTab) => {
      const rest = tabs.filter(id => id !== tab), index = tabs.indexOf(tab);
      change({ tabs: rest, selected: selected === tab ? rest[Math.min(index, rest.length - 1)] || '' : selected, dismissed: [...new Set([...current.dismissed, tab])] });
    },
    show: () => change({ open: true }),
    close: () => change({ open: false, expanded: false }),
    toggleSize: () => change({ expanded: !expanded }),
  };
}
