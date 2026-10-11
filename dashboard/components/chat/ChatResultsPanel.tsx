"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { ChoiceSelect } from "../ChoiceSelect";

import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { useLocale } from "next-intl";
import * as Popover from "@radix-ui/react-popover";
import type { ChatResult } from "../../lib/chatResults";
import { DeliveryEvidence } from "./DeliveryEvidence";
import { Markdown } from "./Markdown";
import { CopyButton } from "./tool-cards/atoms";
import { ChevronLeftIcon, ChevronRightIcon, FileIcon, MessagesIcon, PanelLeftIcon, SaveIcon } from "../icons";
import styles from "./ChatReading.module.css";

export type ResultFocusRequest = { id: string; count: number };
type Heading = { id: string; text: string; level: number };
type Source = { url: string; text: string };

export function ChatResultsPanel({ results, focusRequest, onReveal, selectedId, hidePicker = false, navigation, onOpenFile }: {
  onOpenFile?: (path:string)=>void;
  results: ChatResult[]; focusRequest?: ResultFocusRequest; onReveal?: (id: string) => void;
  selectedId?: string; hidePicker?: boolean; navigation?: ReactNode;
}) {
  const zh = useLocale().startsWith("zh");
  const prefix = useId().replace(/:/g, "");
  const exportDescriptionId = prefix + "-export-description";
  const [chosen, setChosen] = useState("");
  const [view, setView] = useState("preview");
  const [exportError, setExportError] = useState("");
  const [outlineOpen, setOutlineOpen] = useState(false);
  const [headings, setHeadings] = useState<Heading[]>([]);
  const [sources, setSources] = useState<Source[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const bodyRef = useRef<HTMLDivElement>(null);
  const jumping = useRef(false);
  useEffect(() => { if (focusRequest?.id) { setChosen(focusRequest.id); setView("preview"); } }, [focusRequest?.id, focusRequest?.count]);
  const result = results.find((item) => item.id === (selectedId || chosen)) || results[results.length - 1];
  const index = results.findIndex((item) => item.id === result?.id);
  useEffect(() => { scrollRef.current?.scrollTo({ top: 0 }); setExportError(""); setOutlineOpen(false); }, [result?.id]);
  useEffect(() => {
    const root = bodyRef.current;
    if (!root) { setHeadings([]); setSources([]); return; }
    // Build navigation from rendered Markdown, so code fences never become headings.
    setHeadings(Array.from(root.querySelectorAll<HTMLElement>("h1,h2,h3")).map((el, i) => {
      el.id = `result-${prefix}-${i}`; el.tabIndex = -1;
      return { id: el.id, text: el.textContent || "", level: Number(el.tagName.slice(1)) };
    }));
    setSources([...new Map(Array.from(root.querySelectorAll<HTMLAnchorElement>("a[href]")).filter((a) => /^https?:/i.test(a.getAttribute("href") || "")).map((a) => [a.href, { url: a.href, text: a.textContent || a.href }])).values()]);
  }, [result?.id, result?.text, prefix]);
  function jump(id: string) {
    const el = bodyRef.current?.querySelector<HTMLElement>(`[id="${id}"]`), scroll = scrollRef.current;
    if (!el || !scroll) return;
    jumping.current = true; setOutlineOpen(false); setView("preview");
    requestAnimationFrame(() => {
      scroll.scrollTo({ top: scroll.scrollTop + el.getBoundingClientRect().top - scroll.getBoundingClientRect().top - 20 });
      el.focus({ preventScroll: true });
    });
  }
  function download() {
    if (!result) return;
    try {
      const url = URL.createObjectURL(new Blob([result.text], { type: "text/markdown;charset=utf-8" }));
      const anchor = document.createElement("a");
      anchor.href = url; anchor.download = `${result.title.replace(/[\\/:*?"<>|\u0000-\u001f]/g, "-").slice(0, 90) || "result"}.md`;
      document.body.appendChild(anchor); anchor.click(); anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch { setExportError(i18nCopy(zh, "copy.components_chat_ChatResultsPanel.001")); }
  }
  if (!result) return <div className="flex h-full items-center justify-center p-8 text-center" data-testid="canvas-results-empty">
    <div className="max-w-md"><FileIcon size={24} className="mx-auto mb-4 text-[color:var(--text-muted)]" /><h2 className="text-base font-medium">{i18nCopy(zh, "copy.components_chat_ChatResultsPanel.002")}</h2>
      <p className="mt-3 text-sm leading-relaxed text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_ChatResultsPanel.003")}</p>
    </div>
  </div>;
  const revealLabel = result.agentId ? (i18nCopy(zh, "copy.components_chat_ChatResultsPanel.004")) : (i18nCopy(zh, "copy.components_chat_ChatResultsPanel.005"));
  const hasHeading = /^\s{0,3}#{1,6}\s/.test(result.text.trimStart());
  return <div className={styles.reader} data-testid="canvas-results">
    <div className={styles.toolbar} data-testid="result-toolbar">
      {navigation || (!hidePicker ? <div data-preview-picker>
        <ChoiceSelect aria-label={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.006")} value={result.id} onValueChange={(value) => { setChosen(value); setView("preview"); }}>
          {[...results].reverse().map((item) => <option value={item.id} key={item.id}>{item.title}</option>)}
        </ChoiceSelect>
        <button type="button" aria-label={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.007")} disabled={index <= 0} onClick={() => setChosen(results[index - 1].id)}><ChevronLeftIcon size={14} /></button>
        <button type="button" aria-label={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.008")} disabled={index >= results.length - 1} onClick={() => setChosen(results[index + 1].id)}><ChevronRightIcon size={14} /></button>
      </div> : <span className="min-w-0 flex-1 truncate text-xs" title={result.title}>{result.title}</span>)}
      <div className={styles.tools}>
        <div className="flex" role="group" aria-label={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.009")}>
          <button type="button" aria-label={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.010")} title={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.011")} aria-pressed={view === "preview"} onClick={() => setView("preview")}><FileIcon size={14} /></button>
          <button type="button" aria-label="Markdown" title="Markdown" aria-pressed={view === "markdown"} onClick={() => setView("markdown")}>MD</button>
        </div>
        <Popover.Root open={outlineOpen} onOpenChange={setOutlineOpen}>
          <Popover.Trigger asChild><button type="button" aria-label={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.012")} title={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.013")} disabled={!headings.length && !sources.length}><PanelLeftIcon size={14} /></button></Popover.Trigger>
          <Popover.Portal><Popover.Content align="end" sideOffset={6} collisionPadding={8} className={styles.outline} aria-label={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.014")}
            onCloseAutoFocus={(e) => { if (jumping.current) { e.preventDefault(); jumping.current = false; } }}>
            {headings.length ? <nav aria-label={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.015")}><h3>{i18nCopy(zh, "copy.components_chat_ChatResultsPanel.016")}</h3>{headings.map((h) => <button key={h.id} type="button" onClick={() => jump(h.id)} style={{ paddingLeft: h.level > 2 ? 20 : 8 }}>{h.text}</button>)}</nav> : null}
            {sources.length ? <section className="mt-2 border-t border-[color:var(--line)] pt-2" aria-label={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.017")}><h3>{i18nCopy(zh, "copy.components_chat_ChatResultsPanel.018")} ({sources.length})</h3>{sources.map((source) => <a key={source.url} href={source.url} target="_blank" rel="noopener noreferrer">{source.text}<span className="block text-xs text-[color:var(--text-muted)]">{new URL(source.url).hostname}</span></a>)}</section> : null}
          </Popover.Content></Popover.Portal>
        </Popover.Root>
        <CopyButton text={result.text} />
        <button type="button" onClick={download} aria-label={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.025")} title={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.026")} aria-describedby={exportDescriptionId}><SaveIcon size={14} /></button>
        {onReveal ? <button type="button" onClick={() => onReveal(result.id)} aria-label={revealLabel} title={revealLabel}><MessagesIcon size={14} /></button> : null}
      </div>
    </div>
    <p id={exportDescriptionId} className="px-4 py-2 text-xs text-[color:var(--text-muted)]">{i18nCopy(zh, "copy.components_chat_ChatResultsPanel.027")}</p>
    {exportError ? <p role="alert" className="px-4 py-2 text-xs text-danger">{exportError}</p> : null}
    <div ref={scrollRef} hidden={view !== "preview"} role="region" aria-label={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.021")} className={view === "preview" ? styles.body : "hidden"}>
      <article className={styles.article} data-result-id={result.id}>
        {!hasHeading ? <h2 className={styles.title}>{result.title}</h2> : null}
        <DeliveryEvidence result={result} onOpenFile={onOpenFile} onReveal={onReveal?()=>onReveal(result.id):undefined} />
        <div ref={bodyRef} data-testid="canvas-result-body"><Markdown>{result.text}</Markdown></div>
        <footer className={styles.provenance}>{result.agentId ? (i18nCopy(zh, "copy.components_chat_ChatResultsPanel.022", { value0: result.attempt })) : (i18nCopy(zh, "copy.components_chat_ChatResultsPanel.023"))}</footer>
      </article>
    </div>
    <div hidden={view !== "markdown"} role="region" aria-label="Markdown" className={view === "markdown" ? "min-h-0 flex-1 p-4" : "hidden"}>
      {view === "markdown" ? <textarea readOnly aria-label={i18nCopy(zh, "copy.components_chat_ChatResultsPanel.024")} value={result.text} className="h-full w-full resize-none rounded-md border border-[color:var(--line)] bg-transparent p-3 font-mono text-xs leading-relaxed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-400" /> : null}
    </div>
  </div>;
}
