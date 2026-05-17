"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { authHeaders, isLocalDashboardHost } from "../lib/auth";
import { callApi, clientApi } from "../lib/clientApi";
import { useDialogs } from "../lib/dialogs";
import { formatTime, timezoneLabel } from "../lib/format";
import { useUiSettings } from "../lib/settings";
import { AccountSelector } from "./AccountSelector";
import {
  BellIcon,
  PowerIcon,
  SearchIcon,
  SettingsIcon,
  StarIcon,
} from "./icons";
import { NeryaLogo } from "./NeryaLogo";

type Workspace = {
  live_trading_enabled?: boolean;
  kill_switch?: boolean;
  root?: string;
};

function safeTitleTranslate(t: (k: string) => string, path: string): string | null {
  try {
    const v = t(path);
    if (!v) return null;
    if (v === path) return null;
    if (v.startsWith("nav.")) return null;
    return v;
  } catch {
    return null;
  }
}

function fallbackTitle(path: string): string {
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
  const tNav = useTranslations("nav");
  const tHeader = useTranslations("topHeader");
  const tCommon = useTranslations("common");
  const { confirm, toast } = useDialogs();
  const title = safeTitleTranslate(tNav, pathname) ?? fallbackTitle(pathname);
  const now = useClock();
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [online, setOnline] = useState<boolean>(true);
  const [inboxNeedsAction, setInboxNeedsAction] = useState<number>(0);
  const [restartBusy, setRestartBusy] = useState<boolean>(false);
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
  const healthTextClass =
    systemHealth === "ok"
      ? "text-emerald-500"
      : systemHealth === "warn"
      ? "text-amber-500"
      : "text-rose-500";
  // The /workspace probe may have already flipped us offline before
  // /operator/overview returns; collapse them into a single text label.
  const healthLabel = online
    ? systemHealth === "ok"
      ? tCommon("online")
      : systemHealth === "warn"
      ? tHeader("workspaceWarn")
      : tHeader("workspaceBlocked")
    : tCommon("offline");
  const canRestartLocal = isLocalDashboardHost();

  // Hide on the chat page because it uses a full-height custom layout.
  if (pathname.startsWith("/chat")) return null;

  async function waitForRuntimeRecovery(): Promise<void> {
    const startedAt = Date.now();
    const deadlineMs = 90_000;
    while (Date.now() - startedAt < deadlineMs) {
      try {
        const res = await fetch("/api/proxy/health", {
          cache: "no-store",
          headers: authHeaders(),
        });
        if (res.ok) return;
      } catch {
        // Expected while the dashboard/API are restarting.
      }
      await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    throw new Error(tHeader("restartTimeout"));
  }

  async function handleRestartClick() {
    if (!canRestartLocal || restartBusy) return;
    const confirmed = await confirm({
      title: tHeader("restartTitle"),
      message: tHeader("restartConfirm"),
      okLabel: tHeader("restartNow"),
      cancelLabel: tCommon("cancel"),
      tone: "danger",
    });
    if (!confirmed) return;

    setRestartBusy(true);
    toast({ message: tHeader("restartStarted"), tone: "warn", durationMs: 5000 });

    try {
      const res = await fetch("/api/system/restart", {
        method: "POST",
        headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify({
          workspace: workspace?.root || "",
          apiPort: 18317,
        }),
        cache: "no-store",
      });
      const payload = (await res.json().catch(() => null)) as
        | { detail?: string; error?: string }
        | null;
      if (!res.ok) {
        throw new Error(payload?.detail || payload?.error || tHeader("restartFailed"));
      }
      await waitForRuntimeRecovery();
      toast({ message: tHeader("restartRecovered"), tone: "ok", durationMs: 2500 });
      window.setTimeout(() => window.location.reload(), 600);
    } catch (error) {
      setRestartBusy(false);
      toast({
        message: error instanceof Error ? error.message : tHeader("restartFailed"),
        tone: "error",
        durationMs: 7000,
      });
    }
  }

  return (
    <header
      className="sticky top-0 z-40 backdrop-blur-xl border-b"
      style={{
        background: "var(--header-bg, rgba(4,4,13,0.65))",
        borderColor: "var(--line)",
      }}
    >
      <div className="px-6 lg:px-10 py-3 flex items-center gap-4">
        <div className="flex items-center gap-2 shrink-0">
          <h1 className="text-[18px] font-medium text-[color:var(--text-base)] whitespace-nowrap">
            {title}
          </h1>
          <button
            className="text-ink-500 hover:text-amber-500 transition-colors"
            title={tHeader("pinToFavorites")}
          >
            <StarIcon size={16} />
          </button>
        </div>

        <div className="ml-auto flex items-center gap-3">
          <AccountSelector />

          {/* Clock + system health collapsed into a single low-key strip.
              No pulse dot; offline state is conveyed by color alone. */}
          <div
            className="hidden md:flex items-center gap-2 text-[12px] text-[color:var(--text-muted)]"
            title={
              online
                ? tHeader("workspaceStatus", { status: healthLabel })
                : tHeader("backendUnreachable")
            }
          >
            <span className="font-mono tabular-nums" suppressHydrationWarning>
              {now ?? "—"}
            </span>
            <span aria-hidden className="opacity-50">·</span>
            <span className={online ? healthTextClass : "text-rose-500"}>
              {healthLabel}
            </span>
          </div>

          <button className="icon-btn" title={tHeader("search")}>
            <SearchIcon size={16} />
          </button>
          <Link
            href="/inbox"
            className="icon-btn relative"
            title={
              inboxNeedsAction > 0
                ? tHeader("itemsNeedAttention", { count: inboxNeedsAction })
                : tHeader("actionInbox")
            }
          >
            <BellIcon size={16} />
            {inboxNeedsAction > 0 ? (
              <span className="absolute -top-1 -right-1 min-w-[16px] h-4 px-1 rounded-full bg-rose-500 text-white text-[10px] flex items-center justify-center">
                {inboxNeedsAction > 99 ? "99+" : inboxNeedsAction}
              </span>
            ) : null}
          </Link>
          {canRestartLocal ? (
            <button
              className="icon-btn text-rose-300 hover:text-white hover:bg-rose-500/15 disabled:opacity-60 disabled:cursor-not-allowed"
              title={restartBusy ? tHeader("restartInProgress") : tHeader("restart")}
              aria-label={
                restartBusy ? tHeader("restartInProgress") : tHeader("restart")
              }
              onClick={() => void handleRestartClick()}
              disabled={restartBusy}
            >
              <PowerIcon
                size={16}
                className={restartBusy ? "animate-pulse" : undefined}
              />
            </button>
          ) : null}
          <Link href="/settings" className="icon-btn" title={tHeader("settings")}>
            <SettingsIcon size={16} />
          </Link>
          <div className="w-8 h-8 rounded-full overflow-hidden bg-black/30 flex items-center justify-center">
            <NeryaLogo size={32} />
          </div>
        </div>
      </div>
    </header>
  );
}
