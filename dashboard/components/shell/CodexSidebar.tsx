"use client";

/**
 * CodexSidebar — the persistent left rail for the Codex-inspired shell.
 *
 * Layout mirrors the Codex desktop app:
 *
 *   ┌──────────────┐
 *   │ Nerya     [⟨] │  brand + collapse
 *   │ ▦ Overview    │  ── core daily drivers only
 *   │ ✎ New chat    │
 *   │ ⌕ Search   ⌘K │
 *   │ ◇ Agents      │  (agents / skills / tasks share one page)
 *   │ ◔ Strategies  │
 *   │ ▸ More…       │  collapsible: Trading / Automation +
 *   │               │  capability-gated routes
 *   │ CHATS         │  ── recent conversations (folded-in rail)
 *   │ ▭ alpha plan  │
 *   │ ⚙ Settings    │  ── footer (settings only)
 *   └──────────────┘
 *
 * Destinations still flow from ``/operator/nav`` so capability gating +
 * badges keep working; this component is purely the presentation layer
 * that re-shapes them into the Codex information architecture. Low-frequency
 * routes fold under "More"; notifications live in the top-right shell bell
 * (ShellNotifications), not in this rail.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState, type ComponentType } from "react";
import type { SVGProps } from "react";
import { clientApi } from "../../lib/clientApi";
import type { NavEntry } from "../../lib/operatorTypes";
import {
  loadThreads,
  subscribeThreadsChanged,
  deleteThreadLocally,
  type ChatThread,
} from "../../lib/chat";
import { confirm as confirmDialog } from "../../lib/dialogs";
import { useOperatorNav } from "../../lib/useOperatorNav";
import { NeryaLogo } from "../NeryaLogo";
import { useCommandPalette } from "./CommandPalette";
import { SidebarStrategies } from "./SidebarStrategies";
import {
  AgentsIcon,
  ChevronDownIcon,
  ComposeIcon,
  MessagesIcon,
  NAV_ICONS,
  NAV_ICON_BY_NAME,
  OverviewIcon,
  PanelLeftIcon,
  PortfolioIcon,
  SearchIcon,
  SettingsIcon,
  StrategiesIcon,
  TrashIcon,
  TriggersIcon,
} from "../icons";

type IconComp = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;

const COLLAPSE_KEY = "nerya.sidebar.collapsed";
const ADVANCED_OPEN_KEY = "nerya.sidebar.advanced-open";

/** Destinations already surfaced explicitly — kept out of the "More" list. */
const COVERED_HREFS = new Set([
  "/chat",
  "/agents",
  "/skills",
  "/tasks",
  "/workflows",
  "/dashboard",
  "/portfolio",
  "/strategies",
  "/inbox",
  "/settings",
  "/accounts",
  "/orders",
  "/incidents",
]);

/** Settings owns these integration tools in its dedicated rail. Keeping them
 * out of "More" avoids two sidebar entries opening the same page. */
const SETTINGS_TOOL_HREFS = new Set([
  "/memory",
  "/web-search",
  "/browsers",
  "/env-vault",
  "/gateway",
]);

function pathMatches(pathname: string, href: string): boolean {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

function isActive(pathname: string, href: string, matches?: string[]): boolean {
  return [href, ...(matches ?? [])].some((h) => pathMatches(pathname, h));
}

function resolveNavIcon(item: NavEntry): IconComp | null {
  if (item.icon && NAV_ICON_BY_NAME[item.icon]) return NAV_ICON_BY_NAME[item.icon];
  return NAV_ICONS[item.href] ?? null;
}

function safeNavTranslate(
  t: (key: string) => string,
  href: string,
  fallback: string,
): string {
  try {
    const v = t(href);
    if (!v || v === href || v.startsWith("nav.")) return fallback;
    return v;
  } catch {
    return fallback;
  }
}

function SideRow({
  icon: Icon,
  label,
  active,
  collapsed,
  badge,
  shortcut,
  href,
  onClick,
}: {
  icon: IconComp;
  label: string;
  active?: boolean;
  collapsed: boolean;
  badge?: number;
  shortcut?: string;
  href?: string;
  onClick?: () => void;
}) {
  const inner = (
    <>
      <Icon
        size={16}
        className={`shrink-0 ${active ? "text-brand-200" : "text-[color:var(--text-muted)] group-hover:text-[color:var(--text-base)]"}`}
      />
      {!collapsed ? (
        <>
          <span className="truncate">{label}</span>
          {typeof badge === "number" && badge > 0 ? (
            <span className="ml-auto inline-flex h-5 min-w-[20px] items-center justify-center rounded-full bg-rose-500/15 px-1.5 text-[11px] text-rose-400">
              {badge > 99 ? "99+" : badge}
            </span>
          ) : shortcut ? (
            <kbd className="ml-auto cmdk-kbd">{shortcut}</kbd>
          ) : null}
        </>
      ) : null}
    </>
  );

  const cls = [
    "group sidebar-item w-full",
    active ? "sidebar-item-active" : "sidebar-item-idle",
    collapsed ? "justify-center px-0" : "",
  ].join(" ");

  if (href) {
    return (
      <Link href={href} className={cls} title={collapsed ? label : undefined}>
        {inner}
      </Link>
    );
  }
  return (
    <button type="button" onClick={onClick} className={cls} title={collapsed ? label : undefined}>
      {inner}
    </button>
  );
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-3 pb-1 pt-1 text-[11px] font-medium uppercase tracking-wide text-[color:var(--text-muted)]">
      {children}
    </div>
  );
}

export function CodexSidebar() {
  const pathname = usePathname() || "/";
  const t = useTranslations("sidebar");
  const tNav = useTranslations("nav");
  const tChat = useTranslations("chat");
  const tCommon = useTranslations("common");
  const palette = useCommandPalette();
  const nav = useOperatorNav();

  const [collapsed, setCollapsed] = useState(false);
  const [isNarrow, setIsNarrow] = useState(false);
  const [hydrated, setHydrated] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [chats, setChats] = useState<ChatThread[]>([]);

  useEffect(() => {
    try {
      if (localStorage.getItem(COLLAPSE_KEY) === "1") setCollapsed(true);
      if (localStorage.getItem(ADVANCED_OPEN_KEY) === "1") setAdvancedOpen(true);
    } catch {
      /* ignore */
    }
    setHydrated(true);
  }, []);

  useEffect(() => {
    const query = window.matchMedia("(max-width: 720px)");
    const sync = () => setIsNarrow(query.matches);
    sync();
    query.addEventListener("change", sync);
    return () => query.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [collapsed, hydrated]);

  useEffect(() => {
    if (!hydrated) return;
    try {
      localStorage.setItem(ADVANCED_OPEN_KEY, advancedOpen ? "1" : "0");
    } catch {
      /* ignore */
    }
  }, [advancedOpen, hydrated]);

  // Recent conversations live here now (the chat view no longer carries
  // its own rail). ChatView broadcasts on every save/delete, so we just
  // re-read the local store whenever it changes. Threads bound to a
  // strategy render in the SidebarStrategies block instead of the flat
  // CHATS list, so they're filtered out here.
  useEffect(() => {
    const read = () =>
      setChats(
        loadThreads()
          .filter((t) => !t.strategy_id)
          .sort((a, b) => (b.updated_ts || 0) - (a.updated_ts || 0)),
      );
    read();
    return subscribeThreadsChanged(read);
  }, []);

  async function removeChat(id: string) {
    const ok = await confirmDialog({ message: tChat("deleteConfirm"), tone: "danger" });
    if (!ok) return;
    deleteThreadLocally(id); // tombstone + persist + broadcast (ChatView leaves dead route)
    void clientApi.sessionDelete(id).catch(() => {
      /* keep the local tombstone even if the backend delete fails */
    });
  }

  const advancedItems = useMemo(() => {
    const seen = new Set<string>();
    const out: NavEntry[] = [];
    for (const item of [...nav.data.primary, ...nav.data.advanced]) {
      if (
        COVERED_HREFS.has(item.href) ||
        SETTINGS_TOOL_HREFS.has(item.href) ||
        seen.has(item.href)
      ) {
        continue;
      }
      seen.add(item.href);
      out.push(item);
    }
    return out;
  }, [nav.data]);

  const railCollapsed = collapsed || isNarrow;
  const width = railCollapsed ? (isNarrow ? "w-14" : "w-[68px]") : "w-64";

  return (
    <aside
      className={`${width} nerya-sidebar sticky top-0 flex h-screen shrink-0 flex-col overflow-hidden border-r transition-[width] duration-200`}
      style={{ background: "var(--panel-bg)", borderColor: "var(--line)" }}
    >
      {/* Brand + collapse */}
      <div className="flex items-center gap-2.5 px-3 py-3">
        <Link href="/" className="flex min-w-0 items-center gap-2" aria-label="Nerya">
          <span className="flex h-7 w-7 shrink-0 items-center justify-center overflow-hidden rounded-lg">
            <NeryaLogo size={26} />
          </span>
          {!railCollapsed ? (
            <span className="truncate text-[14px] font-semibold text-[color:var(--text-base)]">
              {t("brandName")}
            </span>
          ) : null}
        </Link>
        {!railCollapsed ? (
          <button
            type="button"
            onClick={() => setCollapsed(true)}
            className="ml-auto inline-flex h-7 w-7 items-center justify-center rounded-md text-[color:var(--text-muted)] transition-colors hover:bg-brand-500/10 hover:text-[color:var(--text-base)]"
            title={t("collapse")}
            aria-label={t("collapse")}
          >
            <PanelLeftIcon size={17} />
          </button>
        ) : null}
      </div>

      {railCollapsed && !isNarrow ? (
        <button
          type="button"
          onClick={() => setCollapsed(false)}
          className="mx-auto mb-1 inline-flex h-7 w-7 items-center justify-center rounded-md text-[color:var(--text-muted)] transition-colors hover:bg-brand-500/10 hover:text-[color:var(--text-base)]"
          title={t("expand")}
          aria-label={t("expand")}
        >
          <PanelLeftIcon size={17} />
        </button>
      ) : null}

      <nav className="flex min-h-0 flex-1 flex-col gap-3 px-2 pb-2">
        {/* Codex-minimal core: just the daily drivers. Overview leads (it is
            the strategies/positions cockpit), then chat + search, then the
            two destinations that share their tabbed sections: Agents
            (agents/skills/tasks) and Strategies. Everything lower-frequency
            (Trading, Automation, capability-gated routes) folds under a
            single "More". Notifications live in the top-right shell bell. */}
        <div className="shrink-0 space-y-0.5">
          <SideRow icon={OverviewIcon} label={t("overview")} href="/dashboard" collapsed={railCollapsed} active={isActive(pathname, "/dashboard")} />
          <SideRow icon={ComposeIcon} label={t("newChat")} href="/chat" collapsed={railCollapsed} active={isActive(pathname, "/chat")} />
          <SideRow icon={SearchIcon} label={t("search")} collapsed={railCollapsed} shortcut="⌘K" onClick={() => palette.setOpen(true)} />
          <SideRow icon={AgentsIcon} label={t("agents")} href="/agents" collapsed={railCollapsed} active={isActive(pathname, "/agents", ["/skills", "/tasks"])} />
          <SideRow icon={StrategiesIcon} label={t("strategies")} href="/strategies" collapsed={railCollapsed} active={isActive(pathname, "/strategies")} />

          {railCollapsed ? (
            <>
              <SideRow icon={PortfolioIcon} label={t("trading")} href="/portfolio" collapsed active={isActive(pathname, "/portfolio", ["/accounts", "/orders", "/incidents"])} />
              <SideRow icon={TriggersIcon} label={t("automation")} href="/workflows" collapsed active={isActive(pathname, "/workflows")} />
              {advancedItems.map((item) => {
                const Icon = resolveNavIcon(item) ?? OverviewIcon;
                return (
                  <SideRow
                    key={item.href}
                    icon={Icon}
                    label={safeNavTranslate(tNav, item.href, item.label)}
                    href={item.href}
                    collapsed
                    active={isActive(pathname, item.href, item.match_hrefs)}
                  />
                );
              })}
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={() => setAdvancedOpen((v) => !v)}
                className="group sidebar-item sidebar-item-idle w-full"
              >
                <ChevronDownIcon
                  size={15}
                  className={`shrink-0 text-[color:var(--text-muted)] transition-transform ${advancedOpen ? "" : "-rotate-90"}`}
                />
                <span className="truncate">{t("sectionAdvanced")}</span>
              </button>
              {advancedOpen ? (
                <>
                  <SideRow icon={PortfolioIcon} label={t("trading")} href="/portfolio" collapsed={false} active={isActive(pathname, "/portfolio", ["/accounts", "/orders", "/incidents"])} />
                  <SideRow icon={TriggersIcon} label={t("automation")} href="/workflows" collapsed={false} active={isActive(pathname, "/workflows")} />
                  {advancedItems.map((item) => {
                    const Icon = resolveNavIcon(item) ?? OverviewIcon;
                    return (
                      <SideRow
                        key={item.href}
                        icon={Icon}
                        label={safeNavTranslate(tNav, item.href, item.label)}
                        href={item.href}
                        collapsed={false}
                        active={isActive(pathname, item.href, item.match_hrefs)}
                      />
                    );
                  })}
                </>
              ) : null}
            </>
          )}
        </div>

        {/* Running strategies + chats — one shared scroll region. The
            strategies block pins above the CHATS list so the operator sees
            what's live without opening the strategies page; each strategy
            expands into its second-level sessions (incl. evolution runs). */}
        {!railCollapsed ? (
          <div className="flex min-h-0 flex-1 flex-col">
            <div className="embedded-scroll min-h-0 flex-1 pr-1">
              <SidebarStrategies />
              {chats.length > 0 ? (
                <>
                  <SectionLabel>{t("sectionChats")}</SectionLabel>
                  <div className="space-y-0.5">
                    {chats.map((c) => {
                      const active = pathMatches(pathname, `/chat/${c.id}`);
                      return (
                        <div
                          key={c.id}
                          className={`group sidebar-item pr-1 ${active ? "sidebar-item-active" : "sidebar-item-idle"}`}
                        >
                          <Link
                            href={`/chat/${encodeURIComponent(c.id)}`}
                            className="flex min-w-0 flex-1 items-center gap-2"
                            title={c.title}
                          >
                            <MessagesIcon
                              size={14}
                              className={`shrink-0 ${active ? "text-brand-200" : "text-[color:var(--text-muted)]"}`}
                            />
                            <span className="truncate">{c.title || t("untitledChat")}</span>
                          </Link>
                          <button
                            type="button"
                            onClick={(e) => {
                              e.preventDefault();
                              e.stopPropagation();
                              void removeChat(c.id);
                            }}
                            className="ml-1 shrink-0 rounded p-1 text-[color:var(--text-muted)] opacity-0 transition-opacity hover:text-rose-400 group-hover:opacity-100"
                            title={tCommon("delete")}
                            aria-label={tCommon("delete")}
                          >
                            <TrashIcon size={13} />
                          </button>
                        </div>
                      );
                    })}
                  </div>
                </>
              ) : null}
            </div>
          </div>
        ) : null}
      </nav>

      {/* Footer — theme + language now live in Settings → Interface →
          Appearance (Codex parity), so the rail footer is just Settings. */}
      <div className="space-y-1 border-t px-2 py-2" style={{ borderColor: "var(--line)" }}>
        <SideRow icon={SettingsIcon} label={t("settings")} href="/settings" collapsed={railCollapsed} active={isActive(pathname, "/settings")} />
      </div>
    </aside>
  );
}

export default CodexSidebar;
