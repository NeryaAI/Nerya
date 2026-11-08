import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

type FindSection = "commentary" | "reply";
export type FindEntry = { id: string; text: string; sections?: {section:FindSection; text:string}[] };
export type TimelineMatch = { id: string; matchOffset: number; length: number; occurrence: number; query: string; section?:FindSection };
export type RevealRequest = { session: string; id: string; focus?: boolean; match?: TimelineMatch };
export const REVEAL_CONVERSATION = "nerya:reveal-conversation";
export function requestConversationReveal(request: RevealRequest) {
  window.dispatchEvent(new CustomEvent(REVEAL_CONVERSATION, { detail: request }));
}

/** Literal, case-insensitive occurrences retain UTF-16 offsets, including Unicode case folds. */
export function conversationMatches(entries: FindEntry[], query: string): TimelineMatch[] {
  const value = query.trim();
  if (!value) return [];
  const escaped = Array.from(value, char => ".*+?^$".includes(char) || "{}()|[]\\".includes(char) ? "\\" + char : char).join("");
  const pattern = new RegExp(escaped, "giu");
  return entries.flatMap(entry => {
    let offset = 0;
    return (entry.sections || [{text:entry.text}]).flatMap(part => {
      const section = "section" in part ? part.section : undefined;
      const matches = [...part.text.matchAll(pattern)].map((match, occurrence) => ({
        id:entry.id, matchOffset:offset + match.index!, length:match[0].length, occurrence, query:value,
        ...(section ? {section} : {}),
      }));
      offset += part.text.length + 2;
      return matches;
    });
  });
}

/** A DOM range can cross Markdown spans without mutating React-owned text nodes. */
export function matchRange(element: HTMLElement, match: TimelineMatch): Range | null {
  const roots = [...element.querySelectorAll<HTMLElement>("[data-find-text]")]
    .filter(root => !match.section || root.closest('[data-turn-section="' + match.section + '"]'));
  if (match.section && !roots.length) return null;
  const nodes: Text[] = [];
  for (const root of roots.length ? roots : [element]) {
    const body = root.querySelector<HTMLElement>(".nerya-markdown") || root;
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, { acceptNode: node =>
      node.parentElement?.closest("button,script,style,[hidden]") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
    while (walker.nextNode()) nodes.push(walker.currentNode as Text);
  }
  const matches = conversationMatches([{ id: match.id, text: nodes.map(node => node.data).join("") }], match.query);
  const found = matches[match.occurrence];
  if (!found) return null;
  const range = document.createRange();
  let offset = 0, started = false;
  for (const node of nodes) {
    const end = offset + node.length;
    if (!started && found.matchOffset < end) { range.setStart(node, found.matchOffset - offset); started = true; }
    if (started && found.matchOffset + found.length <= end) { range.setEnd(node, found.matchOffset + found.length - offset); return range; }
    offset = end;
  }
  return null;
}

export function useTimelineReveal({ session, scopeRef, scrollRef, resolveUnit, hasMore, loadingOlder, onOlder }: {
  session: string; scopeRef: RefObject<HTMLDivElement>; scrollRef: RefObject<HTMLDivElement>;
  resolveUnit: (id: string) => string | undefined; hasMore?: boolean; loadingOlder?: boolean; onOlder?: () => void;
}) {
  const [forced, setForced] = useState<string | null>(null);
  const current = useRef({ session, resolveUnit, hasMore, loadingOlder, onOlder });
  current.current = { session, resolveUnit, hasMore, loadingOlder, onOlder };
  const pending = useRef<AbortController | null>(null);
  const clearHighlight = useRef<() => void>(() => {});
  const cancel = useCallback(() => { pending.current?.abort(); pending.current = null; clearHighlight.current(); setForced(null); }, []);
  useEffect(() => cancel, [session, cancel]);
  const reveal = useCallback(async (request: RevealRequest, signal?: AbortSignal): Promise<boolean> => {
    cancel();
    if (request.session !== session || signal?.aborted) return false;
    const controller = new AbortController(); pending.current = controller;
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    let frame = 0, timeout = 0, revealed = false;
    try {
      return await new Promise<boolean>(resolve => {
        const finish = (ok: boolean) => { revealed = ok; cancelAnimationFrame(frame); clearTimeout(timeout); resolve(ok); };
        controller.signal.addEventListener("abort", () => finish(false), { once: true });
        timeout = window.setTimeout(() => finish(false), 5000);
        let loadingRequested = false, scrolled = false;
        const locate = () => {
          if (controller.signal.aborted || current.current.session !== request.session) return finish(false);
          const state = current.current, unitId = state.resolveUnit(request.id);
          if (!unitId) {
            if (!state.hasMore || !state.onOlder) return finish(false);
            if (!loadingRequested && !state.loadingOlder) { loadingRequested = true; state.onOlder(); }
            if (state.loadingOlder) loadingRequested = false;
            frame = requestAnimationFrame(locate); return;
          }
          const unit = [...(scopeRef.current?.querySelectorAll<HTMLElement>("[data-timeline-turn]") || [])].find(el => el.dataset.timelineTurn === unitId);
          if (!unit) { frame = requestAnimationFrame(locate); return; }
          if (!scrolled) { setForced(unitId); unit.scrollIntoView({ block: "center" }); scrolled = true; frame = requestAnimationFrame(locate); return; }
          if (unit.dataset.timelineMounted !== "true") { frame = requestAnimationFrame(locate); return; }
          const target = [...unit.querySelectorAll<HTMLElement>("[data-find-entry],[data-turn-id]")].find(el => el.dataset.findEntry === request.id || el.dataset.turnId === request.id) || unit;
          // A commentary hit belongs to the public execution narration. Open
          // only that section; final text may already be mounted beside it.
          const execution = request.match?.section === "commentary"
            ? target.querySelector<HTMLDetailsElement>('details[data-testid="execution-process"]') : null;
          if (execution && !execution.open) {
            execution.open = true;
            frame = requestAnimationFrame(locate); return;
          }
          const range = request.match ? matchRange(target, request.match) : null;
          const root = scrollRef.current;
          const rect = range?.getBoundingClientRect() || target.getBoundingClientRect();
          if (root) root.scrollTop += rect.top - root.getBoundingClientRect().top - Math.max(24, root.clientHeight / 3);
          target.classList.add("workbench-search-hit");
          target.dataset.findMatchOffset = String(request.match?.matchOffset ?? "");
          const highlightApi = globalThis as typeof globalThis & { Highlight?: new (...ranges: Range[]) => unknown; CSS?: { highlights?: Map<string, unknown> } };
          if (range && highlightApi.Highlight && highlightApi.CSS?.highlights) highlightApi.CSS.highlights.set("nerya-match", new highlightApi.Highlight(range));
          const selection = range ? window.getSelection() : null;
          if (range && selection) { selection.removeAllRanges(); selection.addRange(range); }
          clearHighlight.current = () => { target.classList.remove("workbench-search-hit"); delete target.dataset.findMatchOffset; highlightApi.CSS?.highlights?.delete("nerya-match"); if (range && selection?.rangeCount && selection.getRangeAt(0) === range) selection.removeAllRanges(); };
          if (request.focus !== false) { target.tabIndex = -1; target.focus({ preventScroll: true }); }
          finish(!request.match || Boolean(range));
        };
        locate();
      });
    } finally { signal?.removeEventListener("abort", abort); if (pending.current === controller) { pending.current = null; if (!revealed) setForced(null); } }
  }, [session, cancel, scopeRef, scrollRef]);
  useEffect(() => {
    const handler = (event: Event) => { const request = (event as CustomEvent<RevealRequest>).detail; if (request?.session === session) void reveal(request); };
    window.addEventListener(REVEAL_CONVERSATION, handler);
    return () => window.removeEventListener(REVEAL_CONVERSATION, handler);
  }, [session, reveal]);
  return { forced, reveal, cancel };
}
