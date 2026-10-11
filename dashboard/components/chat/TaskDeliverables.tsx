"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useState } from "react";
import { useLocale } from "next-intl";
import { FileIcon } from "../icons";

export type TaskDeliverable = { id: string; label: string };
const group = (id: string) => id.startsWith("result:") ? "results"
  : id === "strategy" || id.startsWith("backtest:") ? "strategy"
  : id.startsWith("instrument:") || id.startsWith("snapshot:") ? "research"
  : id === "agents" ? "team" : "files";

/** A directory of existing task resources; it never manufactures execution facts. */
export function TaskDeliverables({ items, files, onSelect, onOpenFile, onBrowseFiles }: {
  items: TaskDeliverable[]; files: { path: string; message_id?: string }[];
  onSelect: (id: string) => void; onOpenFile: (path: string) => void; onBrowseFiles: () => void;
}) {
  const zh = useLocale().startsWith("zh");
  const [query, setQuery] = useState("");
  const match = (label: string) => label.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
  const labels: Record<string, string> = { results: i18nCopy(zh, "copy.components_chat_TaskDeliverables.001"),
    strategy: i18nCopy(zh, "copy.components_chat_TaskDeliverables.002"), research: i18nCopy(zh, "copy.components_chat_TaskDeliverables.003"),
    team: i18nCopy(zh, "copy.components_chat_TaskDeliverables.004"), files: i18nCopy(zh, "copy.components_chat_TaskDeliverables.005") };
  const entries = [...new Map(items.map(item => [item.id, item])).values()].filter(item => match(item.label));
  const paths = [...new Set(files.map(file => file.path))].filter(match);
  const action = "flex min-h-11 w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm hover:bg-[color:var(--panel-bg)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--violet)]";
  return <div className="h-full overflow-y-auto p-4" data-testid="task-deliverables">
    <h2 className="text-sm font-medium">{i18nCopy(zh, "copy.components_chat_TaskDeliverables.006")}</h2>
    <p className="mt-1 text-xs leading-relaxed text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_TaskDeliverables.007")}</p>
    <input type="search" aria-label={i18nCopy(zh, "copy.components_chat_TaskDeliverables.008")} placeholder={i18nCopy(zh, "copy.components_chat_TaskDeliverables.009")} value={query} onChange={event => setQuery(event.target.value)} className="input mt-4 min-h-11 w-full"/>
    {Object.entries(labels).map(([key, label]) => { const rows = entries.filter(item => group(item.id) === key);
      return rows.length ? <section key={key} className="mt-5"><h3 className="mb-1 text-xs font-medium text-[color:var(--text-muted)]">{label}</h3>{rows.map(item => <button key={item.id} type="button" className={action} onClick={() => onSelect(item.id)}><FileIcon size={16}/><span className="min-w-0 break-words">{item.label}</span></button>)}</section> : null;
    })}
    {paths.length > 0 && <section className="mt-5"><h3 className="mb-1 text-xs font-medium text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_TaskDeliverables.010")}</h3>{paths.map(path => <button type="button" key={path} className={action} onClick={() => onOpenFile(path)}><FileIcon size={16}/><span className="min-w-0"><span className="block break-words">{path.split("/").pop()}</span><span className="block break-all text-xs text-[color:var(--text-muted)]">{path}</span></span></button>)}</section>}
    {!entries.length && !paths.length && <p role="status" className="py-8 text-sm text-[color:var(--text-muted)]">{query ? (i18nCopy(zh, "copy.components_chat_TaskDeliverables.011")) : (i18nCopy(zh, "copy.components_chat_TaskDeliverables.012"))}</p>}
    <details className="mt-6 border-t border-[color:var(--line)] pt-3"><summary className="cursor-pointer py-2 text-xs text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_TaskDeliverables.013")}</summary><button type="button" className={action} onClick={onBrowseFiles}>{i18nCopy(zh, "copy.components_chat_TaskDeliverables.014")}</button></details>
  </div>;
}
