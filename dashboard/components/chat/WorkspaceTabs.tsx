"use client";

import type { ReactNode } from "react";

export type WorkspaceTab = { id: string; label: string; compactLabel?: string; meta?: ReactNode };

/** One selected panel, roving focus, and stable ids across live updates. */
export function WorkspaceTabs({ id, label, tabs, value, onChange }: {
  id: string; label: string; tabs: WorkspaceTab[]; value: string;
  onChange: (value: string) => void;
}) {
  return <div role="tablist" aria-label={label} aria-orientation="horizontal"
    className="flex min-w-0 shrink-0 gap-1 overflow-x-auto border-b border-[color:var(--line)] px-2">
    {tabs.map((tab, index) => <button key={tab.id} type="button" role="tab"
      id={`${id}-tab-${tab.id}`} aria-controls={`${id}-panel-${tab.id}`}
      aria-label={tab.compactLabel ? tab.label : undefined} aria-selected={value === tab.id} tabIndex={value === tab.id ? 0 : -1}
      onClick={() => onChange(tab.id)}
      onKeyDown={(event) => {
        const next = event.key === "ArrowRight" ? (index + 1) % tabs.length
          : event.key === "ArrowLeft" ? (index + tabs.length - 1) % tabs.length
          : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : -1;
        if (next < 0) return;
        event.preventDefault();
        onChange(tabs[next].id);
        const target = document.getElementById(`${id}-tab-${tabs[next].id}`);
        target?.focus({ preventScroll: true });
        target?.scrollIntoView({ block: "nearest", inline: "nearest" });
      }}
      className={`inline-flex min-h-11 max-w-[18rem] shrink-0 items-center gap-2 border-b-2 px-3 text-[13px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[color:var(--text-muted)] ${value === tab.id ? "border-[color:var(--text-base)] text-[color:var(--text-base)]" : "border-transparent text-[color:var(--text-muted)] hover:text-[color:var(--text-base)]"}`}>
      {tab.compactLabel ? <><span data-tab-label="full" className="truncate">{tab.label}</span><span data-tab-label="compact" className="hidden">{tab.compactLabel}</span></> : <span className="truncate">{tab.label}</span>}{tab.meta ? <span data-tab-meta className="inline-flex items-center">{tab.meta}</span> : null}
    </button>)}
  </div>;
}
