"use client";

import { useEffect, useState } from 'react';

export type TaskDockTab = 'browser' | 'canvas' | 'agents';
type Preference = { open: boolean; expanded: boolean; selected: TaskDockTab };
const KEY = 'nerya.chat.task-dock.v1';
const VALID = new Set(['browser', 'canvas', 'agents']);

function load(): Record<string, Preference> {
  try {
    const data: unknown = JSON.parse(sessionStorage.getItem(KEY) || '{}');
    if (!data || typeof data !== 'object' || Array.isArray(data)) return {};
    return Object.fromEntries(Object.entries(data).filter(([, p]) => p && typeof p.open === 'boolean'
      && typeof p.expanded === 'boolean' && VALID.has(p.selected)).slice(-24));
  } catch { return {}; }
}

/** UI preferences only. Hiding a panel never stops its browser or agent. */
export function useTaskDock(session: string, available: TaskDockTab[]) {
  const [preferences, setPreferences] = useState<Record<string, Preference>>({});
  const [ready, setReady] = useState(false);
  useEffect(() => { setPreferences(load()); setReady(true); }, []);
  const preference = preferences[session];
  const selected = available.includes(preference?.selected) ? preference.selected : available[0];
  const open = ready && !!session && !!selected && (preference?.open ?? true);
  const expanded = open && (preference?.expanded ?? false);
  useEffect(() => {
    if (!ready || !session || !selected || preference) return;
    setPreferences(old => ({ ...old, [session]: { open: true, expanded: false, selected } }));
  }, [ready, session, selected, preference]);
  useEffect(() => {
    if (!ready) return;
    try { sessionStorage.setItem(KEY, JSON.stringify(Object.fromEntries(Object.entries(preferences).slice(-24)))); }
    catch { /* Optional preference. */ }
  }, [preferences, ready]);
  function change(patch: Partial<Preference>) {
    if (!session) return;
    setPreferences(old => ({ ...old, [session]: {
      ...(old[session] || { open: true, expanded: false, selected: selected || 'canvas' }), ...patch,
    } }));
  }
  return { open, expanded, selected,
    select: (tab: TaskDockTab) => change({ open: true, selected: tab }),
    show: () => change({ open: true }),
    close: () => change({ open: false, expanded: false }),
    toggleSize: () => change({ expanded: !expanded }),
  };
}
