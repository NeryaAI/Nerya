"use client";

import type { ReactNode } from "react";
import { Select as PortalSelect } from "../Select";

export function Row({ label, desc, children }: { label: string; desc?: string; children: ReactNode }) {
  return (
    <div className="settings-row flex min-w-0 flex-col gap-2 border-b px-3 py-3 last:border-b-0 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
      <div className="min-w-0 flex-1">
        <div className="text-[13px] font-medium text-[color:var(--text-base)]">{label}</div>
        {desc ? <div className="mt-1 text-xs leading-relaxed text-[color:var(--text-muted)] [overflow-wrap:anywhere]">{desc}</div> : null}
      </div>
      <div className="min-w-0 max-w-full sm:max-w-[56%] sm:shrink-0 sm:text-right [overflow-wrap:anywhere]">{children}</div>
    </div>
  );
}
export function SettingsGroup({ title, description, children }: { title: string; description?: string; children: ReactNode }) {
  return <section className="min-w-0">
    <div className="mb-3 px-0.5">
      <h3 className="text-sm font-semibold text-[color:var(--text-base)]">{title}</h3>
      {description ? <p className="mt-1 text-xs leading-relaxed text-[color:var(--text-muted)]">{description}</p> : null}
    </div>
    <div className="settings-group-panel">{children}</div>
  </section>;
}
export function Field({ label, hint, children }: { label: ReactNode; hint?: ReactNode; children: ReactNode }) {
  return <label className="block min-w-0 text-xs text-[color:var(--text-base)]">
    <span className="flex flex-wrap items-center justify-between gap-2"><span>{label}</span>{hint ? <span className="text-xs text-[color:var(--text-muted)]">{hint}</span> : null}</span>
    <div className="mt-1.5">{children}</div>
  </label>;
}
export function Metric({ label, value, detail, icon }: { label: string; value: ReactNode; detail?: ReactNode; icon: ReactNode }) {
  return <div className="min-w-0 rounded-lg border border-[color:var(--line)] p-3">
    <div className="flex items-center justify-between gap-3"><span className="text-xs text-[color:var(--text-muted)]">{label}</span><span className="text-[color:var(--violet)]">{icon}</span></div>
    <div className="mt-1.5 text-base font-medium tabular-nums text-[color:var(--text-base)]">{value}</div>
    {detail ? <div className="mt-1 text-xs text-[color:var(--text-muted)]">{detail}</div> : null}
  </div>;
}
export function CompactSelect({ value, onChange, options }: { value: string; onChange: (value: string) => void; options: { value: string; label: string }[] }) {
  return <div className="w-full min-w-0 sm:w-44"><PortalSelect value={value} onChange={onChange} options={options} size="sm" /></div>;
}
