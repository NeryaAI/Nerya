"use client";

/**
 * ⌘K command palette — the Codex-style "搜索" surface.
 *
 * Provides a single overlay that blends navigation (jump to any
 * destination), the strategy "projects" list, recent chat threads, and
 * quick actions (start a new chat seeded with the query, run a web
 * search). The provider owns the open state + the global ⌘K / Ctrl-K
 * shortcut; the sidebar's Search row and any other surface call
 * ``useCommandPalette().setOpen(true)``.
 */

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentType,
} from "react";
import type { SVGProps } from "react";
import { clientApi } from "../../lib/clientApi";
import type { StrategyCard } from "../../lib/api";
import { loadThreads, type ChatThread } from "../../lib/chat";
import { setComposeDraft } from "../../lib/composeDraft";
import {
  AgentsIcon,
  BellIcon,
  ChatIcon,
  ComposeIcon,
  GlobeIcon,
  MemoryIcon,
  OverviewIcon,
  PortfolioIcon,
  SearchIcon,
  SettingsIcon,
  SkillsIcon,
  StrategiesIcon,
  TriggersIcon,
} from "../icons";

type IconComp = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;

type PaletteContextValue = {
  open: boolean;
  setOpen: (value: boolean) => void;
  toggle: () => void;
};

const PaletteContext = createContext<PaletteContextValue | null>(null);

export function useCommandPalette(): PaletteContextValue {
  const ctx = useContext(PaletteContext);
  if (!ctx) {
    return { open: false, setOpen: () => {}, toggle: () => {} };
  }
  return ctx;
}

export function CommandPaletteProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const toggle = useCallback(() => setOpen((v) => !v), []);

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((v) => !v);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const value = useMemo(() => ({ open, setOpen, toggle }), [open, toggle]);

  return (
    <PaletteContext.Provider value={value}>
      {children}
      <CommandPalette />
    </PaletteContext.Provider>
  );
}

type PaletteItem = {
  id: string;
  label: string;
  sub?: string;
  hint?: string;
  group: string;
  icon: IconComp;
  keywords?: string;
  onSelect: () => void;
};

function CommandPalette() {
  const { open, setOpen } = useCommandPalette();
  const router = useRouter();
  const t = useTranslations("commandPalette");
  const tNav = useTranslations("sidebar");
  const [query, setQuery] = useState("");
  const [strategies, setStrategies] = useState<StrategyCard[]>([]);
  const [threads, setThreads] = useState<ChatThread[]>([]);
  const [activeIdx, setActiveIdx] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);

  // Reset transient state + focus the field every time the palette opens.
  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActiveIdx(0);
    setThreads(loadThreads().slice(0, 8));
    const handle = window.setTimeout(() => inputRef.current?.focus(), 20);
    let cancelled = false;
    clientApi
      .strategyList()
      .then((res) => {
        if (!cancelled) setStrategies(res.strategies ?? []);
      })
      .catch(() => {
        if (!cancelled) setStrategies([]);
      });
    return () => {
      cancelled = true;
      window.clearTimeout(handle);
    };
  }, [open]);

  const go = useCallback(
    (href: string) => {
      setOpen(false);
      router.push(href);
    },
    [router, setOpen],
  );

  const destinations = useMemo<PaletteItem[]>(() => {
    const rows: { href: string; label: string; icon: IconComp; keywords?: string }[] = [
      { href: "/", label: tNav("home"), icon: OverviewIcon, keywords: "home start build compose" },
      { href: "/chat", label: tNav("newChat"), icon: ComposeIcon, keywords: "chat agent ask" },
      { href: "/dashboard", label: tNav("overview"), icon: OverviewIcon, keywords: "dashboard home" },
      { href: "/portfolio", label: tNav("trading"), icon: PortfolioIcon, keywords: "trading portfolio positions orders accounts" },
      { href: "/strategies", label: tNav("strategies"), icon: StrategiesIcon, keywords: "strategy lab backtest" },
      { href: "/skills", label: tNav("skills"), icon: SkillsIcon, keywords: "skills plugins capabilities tools" },
      { href: "/workflows", label: tNav("automation"), icon: TriggersIcon, keywords: "automation workflow trigger schedule" },
      { href: "/inbox", label: tNav("inbox"), icon: BellIcon, keywords: "inbox approvals notifications" },
      { href: "/agents", label: tNav("agents"), icon: AgentsIcon, keywords: "agents subagents runtime" },
      { href: "/memory", label: tNav("memory"), icon: MemoryIcon, keywords: "memory profile facts" },
      { href: "/web-search", label: tNav("webSearch"), icon: GlobeIcon, keywords: "web search browse" },
      { href: "/settings", label: tNav("settings"), icon: SettingsIcon, keywords: "settings preferences" },
    ];
    return rows.map((row) => ({
      id: `dest:${row.href}`,
      label: row.label,
      group: t("jumpTo"),
      icon: row.icon,
      keywords: row.keywords,
      onSelect: () => go(row.href),
    }));
  }, [go, t, tNav]);

  const strategyItems = useMemo<PaletteItem[]>(
    () =>
      strategies.slice(0, 12).map((s) => ({
        id: `strategy:${s.id}`,
        label: s.title || s.id,
        sub: s.status,
        group: t("projects"),
        icon: StrategiesIcon,
        keywords: `${s.id} ${s.markets?.join(" ") ?? ""}`,
        onSelect: () => go(`/strategies/${encodeURIComponent(s.id)}`),
      })),
    [strategies, go, t],
  );

  const recentItems = useMemo<PaletteItem[]>(
    () =>
      threads.slice(0, 8).map((th) => ({
        id: `thread:${th.id}`,
        label: th.title || t("untitledChat"),
        group: t("recent"),
        icon: ChatIcon,
        onSelect: () => go(`/chat/${th.id}`),
      })),
    [threads, go, t],
  );

  const actionItems = useMemo<PaletteItem[]>(() => {
    const trimmed = query.trim();
    if (!trimmed) return [];
    return [
      {
        id: "action:ask",
        label: t("askNerya", { query: trimmed }),
        group: t("actions"),
        icon: ComposeIcon,
        onSelect: () => {
          setComposeDraft(trimmed);
          go("/chat");
        },
      },
      {
        id: "action:web",
        label: t("searchWeb", { query: trimmed }),
        group: t("actions"),
        icon: GlobeIcon,
        onSelect: () => go(`/web-search?q=${encodeURIComponent(trimmed)}`),
      },
    ];
  }, [query, t, go]);

  const filtered = useMemo<PaletteItem[]>(() => {
    const q = query.trim().toLowerCase();
    const pool = [...destinations, ...strategyItems, ...recentItems];
    const matched = q
      ? pool.filter((item) =>
          `${item.label} ${item.sub ?? ""} ${item.keywords ?? ""}`
            .toLowerCase()
            .includes(q),
        )
      : [...recentItems, ...destinations, ...strategyItems];
    return [...actionItems, ...matched];
  }, [query, destinations, strategyItems, recentItems, actionItems]);

  useEffect(() => {
    setActiveIdx(0);
  }, [query]);

  const grouped = useMemo(() => {
    const order: string[] = [];
    const map = new Map<string, PaletteItem[]>();
    for (const item of filtered) {
      if (!map.has(item.group)) {
        map.set(item.group, []);
        order.push(item.group);
      }
      map.get(item.group)!.push(item);
    }
    return order.map((group) => ({ group, items: map.get(group)! }));
  }, [filtered]);

  if (!open) return null;

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIdx((i) => Math.min(i + 1, filtered.length - 1));
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIdx((i) => Math.max(i - 1, 0));
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      filtered[activeIdx]?.onSelect();
    }
  }

  let runningIndex = -1;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-start justify-center px-4 pt-[12vh]"
      role="dialog"
      aria-modal="true"
      onMouseDown={() => setOpen(false)}
    >
      <div className="absolute inset-0 bg-black/45 backdrop-blur-sm" aria-hidden />
      <div
        className="cmdk-panel relative w-full max-w-xl overflow-hidden"
        onMouseDown={(e) => e.stopPropagation()}
        onKeyDown={onKeyDown}
      >
        <div className="flex items-center gap-2.5 border-b px-4 py-3" style={{ borderColor: "var(--line)" }}>
          <SearchIcon size={18} className="shrink-0 text-[color:var(--text-muted)]" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("placeholder")}
            className="w-full bg-transparent text-[15px] text-[color:var(--text-base)] placeholder:text-[color:var(--text-muted)] focus:outline-none"
          />
          <kbd className="cmdk-kbd">ESC</kbd>
        </div>

        <div ref={listRef} className="embedded-scroll max-h-[56vh] py-2">
          {filtered.length === 0 ? (
            <div className="px-4 py-10 text-center text-[13px] text-[color:var(--text-muted)]">
              {t("noResults")}
            </div>
          ) : (
            grouped.map(({ group, items }) => (
              <div key={group} className="mb-1.5">
                <div className="px-4 pb-1 pt-2 text-[11px] font-medium uppercase tracking-wide text-[color:var(--text-muted)]">
                  {group}
                </div>
                {items.map((item) => {
                  runningIndex += 1;
                  const active = runningIndex === activeIdx;
                  const Icon = item.icon;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      onMouseEnter={() => setActiveIdx(runningIndex)}
                      onClick={() => item.onSelect()}
                      className={`flex w-full items-center gap-3 px-4 py-2 text-left text-[13px] transition-colors ${
                        active
                          ? "bg-brand-500/15 text-[color:var(--text-base)]"
                          : "text-[color:var(--text-muted)] hover:bg-brand-500/8"
                      }`}
                    >
                      <Icon size={16} className="shrink-0 opacity-80" />
                      <span className="flex-1 truncate">{item.label}</span>
                      {item.sub ? (
                        <span className="shrink-0 text-[11px] text-[color:var(--text-muted)]">
                          {item.sub}
                        </span>
                      ) : null}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
