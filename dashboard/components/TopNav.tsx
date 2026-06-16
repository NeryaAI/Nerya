"use client";

/**
 * Unified top navigation bar.
 *
 * Combines the legacy left ``Sidebar`` + horizontal ``TopHeader`` into a
 * single rounded, horizontally scrollable bar:
 *
 *   ┌──────────────────────────────────────────────────────────────┐
 *   │  [Logo]   ( Dashboard | Chat | Trading | … | More ▾ )        │
 *   │                       account · clock · health                │
 *   │                       files · bell · settings · dark · lang   │
 *   └──────────────────────────────────────────────────────────────┘
 *
 * The nav items still come from the operator-nav endpoint (so
 * capability gating + badges keep working). Advanced items collapse
 * into a portal-anchored ``MoreMenu`` so the dropdown isn't clipped
 * by the rail's ``overflow-x-auto`` scroll viewport. AppShell skips
 * this component on /chat because that workspace owns the full viewport.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { authHeaders, isLocalDashboardHost } from "../lib/auth";
import { clientApi } from "../lib/clientApi";
import { useDialogs } from "../lib/dialogs";
import { useUiSettings, type ThemeMode } from "../lib/settings";
import { useOperatorNav } from "../lib/useOperatorNav";
import type { NavEntry } from "../lib/operatorTypes";
import { AccountSelector } from "./AccountSelector";
import { LanguageMenu } from "./LanguageMenu";
import { NeryaLogo } from "./NeryaLogo";
import { FilesDrawer } from "./FilesDrawer";
import { PortalDropdown, useDropdown } from "./PortalDropdown";
import {
  BellIcon,
  ChevronDownIcon,
  FolderIcon,
  MoonIcon,
  NAV_ICONS,
  NAV_ICON_BY_NAME,
  PowerIcon,
  SettingsIcon,
} from "./icons";

type Workspace = {
  live_trading_enabled?: boolean;
  kill_switch?: boolean;
  root?: string;
};

function resolveIcon(item: NavEntry) {
  if (item.icon && NAV_ICON_BY_NAME[item.icon]) {
    return NAV_ICON_BY_NAME[item.icon];
  }
  return NAV_ICONS[item.href] ?? null;
}

function pathMatches(pathname: string, href: string): boolean {
  return pathname === href || (href !== "/" && pathname.startsWith(`${href}/`));
}

function isActiveNavItem(pathname: string, item: NavEntry): boolean {
  return [item.href, ...(item.match_hrefs ?? [])].some((href) =>
    pathMatches(pathname, href),
  );
}

function safeNavTranslate(
  t: (k: string) => string,
  href: string,
  fallback: string,
): string {
  try {
    const v = t(href);
    if (!v) return fallback;
    if (v === href) return fallback;
    if (v.startsWith("nav.")) return fallback;
    return v;
  } catch {
    return fallback;
  }
}

function NavPill({
  item,
  pathname,
  badge,
  tNav,
}: {
  item: NavEntry;
  pathname: string;
  badge?: number;
  tNav: (key: string) => string;
}) {
  const active = isActiveNavItem(pathname, item);
  const Icon = resolveIcon(item);
  const label = safeNavTranslate(tNav, item.href, item.label);
  return (
    <Link
      href={item.href}
      title={item.tagline ?? label}
      className={`topnav-pill cursor-pointer ${active ? "topnav-pill-active" : ""}`}
    >
      {Icon ? (
        <Icon
          size={14}
          className={active ? "text-white" : "text-current opacity-80"}
        />
      ) : null}
      <span className="whitespace-nowrap">{label}</span>
      {typeof badge === "number" && badge > 0 ? (
        <span
          className={`ml-0.5 inline-flex items-center justify-center min-w-[16px] h-4 px-1 rounded-full text-[10px] ${
            active
              ? "bg-white/20 text-white"
              : "bg-rose-500/15 text-rose-400"
          }`}
        >
          {badge > 99 ? "99+" : badge}
        </span>
      ) : null}
    </Link>
  );
}

function MoreMenu({
  items,
  pathname,
  tNav,
  badges,
}: {
  items: NavEntry[];
  pathname: string;
  tNav: (key: string) => string;
  badges?: Record<string, number | undefined>;
}) {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const { open, toggle, close } = useDropdown();
  const t = useTranslations("topnav");

  if (items.length === 0) return null;
  const active = items.some((item) => isActiveNavItem(pathname, item));

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={toggle}
        className={`topnav-pill cursor-pointer ${active ? "topnav-pill-active" : ""}`}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <span>{t("more")}</span>
        <ChevronDownIcon
          size={12}
          className={`transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>
      <PortalDropdown open={open} onClose={close} anchorRef={triggerRef} width={224}>
        {items.map((item) => {
          const Icon = resolveIcon(item);
          const itemActive = isActiveNavItem(pathname, item);
          const label = safeNavTranslate(tNav, item.href, item.label);
          const badge = badges?.[item.id];
          return (
            <Link
              key={item.href}
              href={item.href}
              role="menuitem"
              onClick={close}
              className={`flex items-center gap-2.5 rounded-xl px-3 py-2 text-[13px] cursor-pointer transition-colors ${
                itemActive
                  ? "bg-brand-500/15 text-white"
                  : "text-ink-200 hover:bg-brand-500/10 hover:text-white"
              }`}
            >
              {Icon ? <Icon size={14} className="opacity-80" /> : null}
              <span className="flex-1 truncate">{label}</span>
              {typeof badge === "number" && badge > 0 ? (
                <span className="inline-flex items-center justify-center min-w-[18px] h-4 px-1 rounded-full bg-rose-500/15 text-rose-400 text-[10px]">
                  {badge > 99 ? "99+" : badge}
                </span>
              ) : null}
            </Link>
          );
        })}
      </PortalDropdown>
    </>
  );
}

export function TopNav() {
  const pathname = usePathname() || "/dashboard";
  const tNav = useTranslations("nav");
  const tHeader = useTranslations("topHeader");
  const tCommon = useTranslations("common");
  const tTopNav = useTranslations("topnav");
  const { confirm, toast } = useDialogs();

  const nav = useOperatorNav();

  const [settings, patchSettings] = useUiSettings();
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [online, setOnline] = useState(true);
  const [inboxNeedsAction, setInboxNeedsAction] = useState(0);
  const [browserSessionCount, setBrowserSessionCount] = useState<number | null>(null);
  const [systemHealth, setSystemHealth] = useState<"ok" | "warn" | "blocked" | "error">(
    "ok",
  );
  const [filesOpen, setFilesOpen] = useState(false);
  const [restartBusy, setRestartBusy] = useState(false);
  const themeMenu = useDropdown();
  const themeAnchorRef = useRef<HTMLButtonElement | null>(null);
  const themeOptions: { value: ThemeMode; label: string }[] = [
    { value: "system", label: tTopNav("systemMode") },
    { value: "light", label: tTopNav("lightMode") },
    { value: "dark", label: tTopNav("darkMode") },
  ];
  const themeLabel =
    themeOptions.find((item) => item.value === settings.darkMode)?.label ??
    themeOptions[0]!.label;

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        await clientApi.health();
        if (!cancelled) {
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
  }, []);

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
        // Non-fatal — the workspace probe still drives ``online``.
      }
    }
    load();
    const t = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const res = await clientApi.browserSessionList();
        if (!cancelled) setBrowserSessionCount(res.count ?? 0);
      } catch {
        if (!cancelled) setBrowserSessionCount(null);
      }
    }
    load();
    const t = setInterval(load, 15_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  void workspace;

  const data = nav.data;
  const badges: Record<string, number | undefined> = {
    inbox: inboxNeedsAction,
    browser_session: browserSessionCount ?? undefined,
  };

  // Settings reaches via the gear icon / settings rail, and the action
  // inbox reaches via the bell icon. Settings-owned tools stay out of
  // "More" so the same page never has two navigation buttons.
  const HIDDEN_FROM_RAIL = new Set([
    "settings",
    "inbox",
    "memory",
    "web_search",
    "browsers",
    "env_vault",
    "gateway",
  ]);
  const railPrimary = data.primary.filter((item) => !HIDDEN_FROM_RAIL.has(item.id));
  const moreItems = data.advanced.filter((item) => !HIDDEN_FROM_RAIL.has(item.id));

  const healthTextClass =
    systemHealth === "ok"
      ? "text-emerald-500"
      : systemHealth === "warn"
      ? "text-amber-500"
      : "text-rose-500";
  const healthLabel = online
    ? systemHealth === "ok"
      ? tCommon("online")
      : systemHealth === "warn"
      ? tHeader("workspaceWarn")
      : tHeader("workspaceBlocked")
    : tCommon("offline");
  const canRestartLocal = isLocalDashboardHost();

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
        // Expected while the local runtime is restarting.
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
      let workspaceRoot = workspace?.root || "";
      if (!workspaceRoot) {
        try {
          const ws = await clientApi.workspace();
          workspaceRoot = ws.root || "";
          setWorkspace(ws);
        } catch {
          workspaceRoot = "";
        }
      }
      const res = await fetch("/api/system/restart", {
        method: "POST",
        headers: authHeaders({ "content-type": "application/json" }),
        body: JSON.stringify({
          workspace: workspaceRoot,
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
    <>
      <header
        className="sticky top-2 z-40 mx-2 mt-2 sm:top-3 sm:mx-3 sm:mt-3 lg:mx-6"
        style={{ marginBottom: "8px" }}
      >
        <div className="glass-hi flex flex-wrap items-center gap-2 rounded-2xl px-2.5 py-2 sm:gap-3 sm:px-3.5 md:flex-nowrap md:rounded-full">
          {/* Brand mark — refreshed for the 2026-05 redesign: single
              wordmark, sentence case, no tracking, no eyebrow. */}
          <Link
            href="/dashboard"
            className="flex shrink-0 items-center gap-2 px-1 py-0.5 cursor-pointer group"
            aria-label="Nerya"
          >
            <span className="relative inline-flex h-9 w-9 shrink-0 items-center justify-center sm:h-9 sm:w-9">
              <NeryaLogo size={36} />
            </span>
            <span className="hidden sm:inline text-[15px] font-medium text-[color:var(--text-base)]">
              {tTopNav("brandName")}
            </span>
          </Link>

          {/* Pill nav */}
          <nav className="topnav-rail order-3 mx-0 w-full min-w-0 flex-shrink overflow-x-auto no-scrollbar md:order-none md:mx-auto md:w-auto">
            {railPrimary.map((item) => (
              <NavPill
                key={item.href}
                item={item}
                pathname={pathname}
                badge={badges[item.id as keyof typeof badges]}
                tNav={tNav}
              />
            ))}
            {moreItems.length ? (
              <MoreMenu
                items={moreItems}
                pathname={pathname}
                tNav={tNav}
                badges={badges}
              />
            ) : null}
          </nav>

          {/* Right cluster — distilled to: account selector, system
              health dot, files, action inbox bell, settings, dark-mode,
              language menu. Search input + clock + duplicate profile
              avatar removed in response to operator feedback. */}
          <div className="ml-auto flex max-w-[calc(100%-3rem)] shrink-0 items-center gap-1 sm:max-w-none sm:gap-2">
            <div className="min-w-0">
              <AccountSelector />
            </div>

            <div
              className="hidden xl:flex items-center text-[12px]"
              title={
                online
                  ? tHeader("workspaceStatus", { status: healthLabel })
                  : tHeader("backendUnreachable")
              }
            >
              <span className={online ? healthTextClass : "text-rose-500"}>
                {healthLabel}
              </span>
            </div>

            <button
              type="button"
              onClick={() => setFilesOpen(true)}
              className="topnav-pill-icon cursor-pointer"
              title={tTopNav("files")}
              aria-label={tTopNav("files")}
            >
              <FolderIcon size={16} />
            </button>

            <Link
              href="/inbox"
              className="topnav-pill-icon cursor-pointer relative"
              title={
                inboxNeedsAction > 0
                  ? tHeader("itemsNeedAttention", { count: inboxNeedsAction })
                  : tHeader("actionInbox")
              }
            >
              <BellIcon size={16} />
              {inboxNeedsAction > 0 ? (
                <span className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 px-1 rounded-full bg-rose-500 text-white text-[10px] flex items-center justify-center">
                  {inboxNeedsAction > 99 ? "99+" : inboxNeedsAction}
                </span>
              ) : null}
            </Link>
            <Link
              href="/settings"
              className="topnav-pill-icon cursor-pointer"
              title={tHeader("settings")}
              aria-label={tHeader("settings")}
            >
              <SettingsIcon size={16} />
            </Link>
            <button
              ref={themeAnchorRef}
              type="button"
              onClick={themeMenu.toggle}
              className="topnav-pill-icon cursor-pointer relative"
              title={themeLabel}
              aria-label={themeLabel}
              aria-expanded={themeMenu.open}
              aria-haspopup="menu"
            >
              <MoonIcon size={16} />
            </button>
            <PortalDropdown open={themeMenu.open} onClose={themeMenu.close} anchorRef={themeAnchorRef}>
              {themeOptions.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => {
                    patchSettings({ darkMode: option.value });
                    themeMenu.close();
                  }}
                  className={`w-full flex items-center gap-3 rounded-lg px-3 py-2 text-[13px] text-[color:var(--text-muted)] hover:bg-brand-500/10 hover:text-[color:var(--text-base)] transition-colors ${
                    settings.darkMode === option.value
                      ? "bg-brand-500/15 text-white"
                      : ""
                  }`}
                >
                  <span className="truncate">{option.label}</span>
                  {settings.darkMode === option.value ? <span aria-hidden="true">✓</span> : null}
                </button>
              ))}
            </PortalDropdown>
            {canRestartLocal ? (
              <button
                type="button"
                onClick={() => void handleRestartClick()}
                className="topnav-pill-icon cursor-pointer text-rose-200 hover:text-white hover:bg-rose-500/15 disabled:opacity-60 disabled:cursor-not-allowed"
                title={restartBusy ? tHeader("restartInProgress") : tHeader("restart")}
                aria-label={restartBusy ? tHeader("restartInProgress") : tHeader("restart")}
                disabled={restartBusy}
              >
                <PowerIcon size={16} className={restartBusy ? "animate-pulse" : undefined} />
              </button>
            ) : null}
            <div className="hidden md:block">
              <LanguageMenu />
            </div>
          </div>
        </div>
      </header>
      <FilesDrawer open={filesOpen} onClose={() => setFilesOpen(false)} />
    </>
  );
}

export default TopNav;
