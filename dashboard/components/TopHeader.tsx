"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { callApi, clientApi } from "../lib/clientApi";
import { formatTime, timezoneLabel } from "../lib/format";
import { useUiSettings } from "../lib/settings";
import { AccountSelector } from "./AccountSelector";
import {
  BellIcon,
  NeryaMark,
  SearchIcon,
  SettingsIcon,
  StarIcon,
} from "./icons";

type Workspace = {
  live_trading_enabled?: boolean;
  kill_switch?: boolean;
  root?: string;
};

const TITLES: Record<string, string> = {
  "/dashboard": "Home",
  "/chat": "Agent Workspace",
  "/portfolio": "Portfolio",
  "/strategies": "Strategies",
  "/agents": "Agents",
  "/skills": "Skills",
  "/workflows": "Workflows",
  "/inbox": "Action Inbox",
  "/tasks": "Agent Tasks",
  "/settings": "Settings",
};

function titleFor(path: string): string {
  if (TITLES[path]) return TITLES[path];
  const seg = path.split("/").filter(Boolean)[0];
  if (!seg) return "Nerya";
  return seg
    .split("-")
    .map((s) => s.charAt(0).toUpperCase() + s.slice(1))
    .join(" ");
}

function useClock(): string | null {
  // Hydration-safe: server-rendered HTML has the clock blank, the
  // client mounts and starts ticking. Avoids the Server: "03:03:39"
  // / Client: "03:03:40" mismatch.
  const [ts, setTs] = useState<number | null>(null);
  useEffect(() => {
    setTs(Date.now());
    const t = setInterval(() => setTs(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const [settings] = useUiSettings();
  const tz = settings.timezone;
  if (ts === null) return null;
  return `${formatTime(ts, tz)} (${timezoneLabel(tz)})`;
}

export function TopHeader() {
  const pathname = usePathname() || "/dashboard";
  const title = titleFor(pathname);
  const now = useClock();
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [online, setOnline] = useState<boolean>(true);
  const [inboxNeedsAction, setInboxNeedsAction] = useState<number>(0);
  const [systemHealth, setSystemHealth] = useState<"ok" | "warn" | "blocked" | "error">(
    "ok",
  );

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const ws = await callApi<Workspace>("/workspace");
        if (!cancelled) {
          setWorkspace(ws);
          setOnline(true);
        }
      } catch {
        if (!cancelled) setOnline(false);
      }
    }
    load();
    const t = setInterval(load, 15000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [pathname]);

  // Pull inbox count + workspace health from /operator/overview so the
  // top-level chrome reflects whatever the BFF says actually needs the
  // operator's attention.
  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [inbox, overview] = await Promise.all([
          clientApi.inboxItems({ requires_action: true, limit: 200 }),
          clientApi.operatorOverview(),
        ]);
        if (cancelled) return;
        setInboxNeedsAction(inbox.data?.needs_action ?? 0);
        setSystemHealth(overview.status);
      } catch {
        // Non-fatal; the legacy /workspace probe still drives ``online``.
      }
    }
    load();
    const t = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  // ``workspace`` (live_trading_enabled, kill_switch, root) is still
  // polled to drive the OFFLINE indicator below.
  void workspace;
  const healthColor =
    systemHealth === "ok"
      ? "bg-emerald-500"
      : systemHealth === "warn"
      ? "bg-amber-400"
      : "bg-rose-500";
  const healthLabel = systemHealth.toUpperCase();

  // Hide on the chat page because it uses a full-height custom layout.
  if (pathname.startsWith("/chat")) return null;

  return (
    <header className="sticky top-0 z-40 bg-[rgba(4,4,13,0.65)] backdrop-blur-xl border-b border-white/5">
      <div className="px-6 lg:px-10 py-3 flex items-center gap-4">
        <div className="flex items-center gap-2 shrink-0">
          <h1 className="text-[20px] font-semibold text-white tracking-tight whitespace-nowrap">
            {title}
          </h1>
          <button
            className="text-ink-500 hover:text-[#f5a524] transition-colors"
            title="Pin to favorites"
          >
            <StarIcon size={16} />
          </button>
        </div>

        <div className="ml-auto flex items-center gap-3">
          {/* 04-29 §11 P9 — replaced the global Paper/Live
              toggle with a real account chooser. The chosen id is the
              "operator focus" used by the Home page KPIs and any
              other multi-account surface. */}
          <AccountSelector />

          {/* Clock — rendered client-side only to avoid hydration drift. */}
          <div className="hidden lg:flex items-center gap-2 px-3 py-1.5 rounded-lg border border-brand-500/10 bg-ink-900/50">
            <span className="w-1.5 h-1.5 rounded-full bg-accent-500 animate-pulse" />
            <span className="text-xs font-mono text-ink-200" suppressHydrationWarning>
              {now ?? "—"}
            </span>
          </div>

          <div
            className="hidden md:flex items-center gap-1.5 px-2 py-1 rounded-lg border border-brand-500/10 bg-ink-900/50"
            title={
              online
                ? `Workspace status: ${healthLabel}`
                : "Backend unreachable"
            }
          >
            <span
              className={`w-1.5 h-1.5 rounded-full ${
                online ? healthColor : "bg-ink-500"
              } ${online ? "animate-pulse" : ""}`}
            />
            <span className="text-[10px] font-mono uppercase tracking-widest text-ink-300">
              {online ? healthLabel : "OFFLINE"}
            </span>
          </div>

          <button className="icon-btn" title="Search">
            <SearchIcon size={16} />
          </button>
          <Link
            href="/inbox"
            className="icon-btn relative"
            title={
              inboxNeedsAction > 0
                ? `${inboxNeedsAction} item(s) need attention`
                : "Action Inbox"
            }
          >
            <BellIcon size={16} />
            {inboxNeedsAction > 0 ? (
              <span className="absolute -top-1 -right-1 min-w-[16px] h-4 px-1 rounded-full bg-[#ef4560] text-white text-[9px] font-mono flex items-center justify-center ring-2 ring-[#0a0b1a]">
                {inboxNeedsAction > 99 ? "99+" : inboxNeedsAction}
              </span>
            ) : online ? null : (
              <span className="absolute top-1 right-1 w-2 h-2 rounded-full bg-ink-500 ring-2 ring-[#0a0b1a]" />
            )}
          </Link>
          <Link href="/settings" className="icon-btn" title="Settings">
            <SettingsIcon size={16} />
          </Link>
          <div className="w-9 h-9 rounded-lg bg-gradient-to-br from-brand-500/30 to-brand-700/40 ring-1 ring-brand-500/40 flex items-center justify-center shadow-glow">
            <NeryaMark size={18} />
          </div>
        </div>
      </div>
    </header>
  );
}
