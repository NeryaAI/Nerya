"use client";
import { AssistantSidebar } from "./AssistantSidebar";

/** Destination rail and conversation tree. */
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState, type ComponentType } from "react";
import type { SVGProps } from "react";
import * as Menu from "@radix-ui/react-dropdown-menu";
import * as Tooltip from "@radix-ui/react-tooltip";
import styles from "./CodexSidebar.module.css";

import type { NavEntry } from "../../lib/operatorTypes";
import { startDesktopDragging } from "../../lib/desktop";
import { useOperatorNav } from "../../lib/useOperatorNav";
import { NeryaLogo } from "../NeryaLogo";


import { useCommandPalette } from "./CommandPalette";
import { SidebarStrategies } from "./SidebarStrategies";
import {
  AgentsIcon,

  ComposeIcon,
  NAV_ICONS,
  NAV_ICON_BY_NAME,
  OverviewIcon,
  PanelLeftIcon,
  PortfolioIcon,
  SearchIcon,
  SettingsIcon,
  StrategiesIcon,
  SkillsIcon,
  MoreIcon,
  TriggersIcon,
} from "../icons";

type IconComp = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;

function SidebarHint({ label, children }: { label: string; children: React.ReactElement }) {
  return <Tooltip.Provider delayDuration={200}><Tooltip.Root>
    <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
    <Tooltip.Portal><Tooltip.Content side="right" sideOffset={8} collisionPadding={8} className={styles.tooltip}>
      {label}
    </Tooltip.Content></Tooltip.Portal>
  </Tooltip.Root></Tooltip.Provider>;
}

const COLLAPSE_KEY = "nerya.sidebar.collapsed";

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
  "/factors",
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
        size={18}
        className={`shrink-0 ${active ? "text-brand-200" : "text-[color:var(--text-muted)] group-hover:text-[color:var(--text-base)]"}`}
      />
      {!collapsed ? (
        <>
          <span className="truncate">{label}</span>
          {typeof badge === "number" && badge > 0 ? (
            <span className="ml-auto inline-flex h-5 min-w-[20px] items-center justify-center rounded-full bg-danger/15 px-1.5 text-[11px] text-danger">
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

  const control = href ? (
      <Link href={href} className={cls} aria-label={label} aria-current={active ? "page" : undefined}>
        {inner}
      </Link>
    ) : (
    <button type="button" onClick={onClick} className={cls} aria-label={label} data-navigation-action>
      {inner}
    </button>
  );
  return collapsed ? <SidebarHint label={label}>{control}</SidebarHint> : control;
}

export function CodexSidebar({ inDrawer = false }: { inDrawer?: boolean }) {
  const pathname = usePathname() || "/";
  const t = useTranslations("sidebar");
  const tNav = useTranslations("nav");


  const palette = useCommandPalette();
  const nav = useOperatorNav();

  const [collapsed, setCollapsed] = useState(false);
  const [isNarrow, setIsNarrow] = useState(false);
  const [hydrated, setHydrated] = useState(false);


  useEffect(() => {
    try {
      if (localStorage.getItem(COLLAPSE_KEY) === "1") setCollapsed(true);
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

  const moreActive = isActive(pathname, "/factors")
    || isActive(pathname, "/workflows")
    || advancedItems.some((item) => isActive(pathname, item.href, item.match_hrefs));

  const railCollapsed = !inDrawer && (collapsed || isNarrow);
  const width = railCollapsed ? "w-14" : "w-[280px]";

  return (
    <aside
      className={`${styles.sidebar} ${width} nerya-sidebar sticky top-0 flex h-dvh shrink-0 flex-col overflow-hidden border-r`}
      style={{ background: "var(--panel-bg)", borderColor: "var(--line)" }}
    >
      <div className="nerya-sidebar-titlebar-drag shrink-0" aria-hidden="true"
        onMouseDown={(event) => { if (event.button === 0) void startDesktopDragging().catch(() => undefined); }} />
      <div className="flex min-h-0 flex-1">
        <nav aria-label={t("brandName")} className="flex w-12 shrink-0 flex-col gap-3 px-1.5 py-3">
          <Link href="/" aria-label="Nerya" className="mx-auto mb-1"><NeryaLogo size={26} /></Link>
          <div className="min-h-0 flex-1 space-y-2 overflow-y-auto">
            <SideRow icon={OverviewIcon} label={t("overview")} href="/dashboard" collapsed active={isActive(pathname,"/dashboard")} />
            <SideRow icon={ComposeIcon} label={t("newChat")} href="/chat" collapsed active={pathMatches(pathname,"/chat")} />
            <SideRow icon={AgentsIcon} label={t("agents")} href="/agents" collapsed active={isActive(pathname,"/agents",["/skills","/tasks"])} />
            <SideRow icon={StrategiesIcon} label={t("strategies")} href="/strategies" collapsed active={isActive(pathname,"/strategies")} />
            <SideRow icon={PortfolioIcon} label={t("trading")} href="/portfolio" collapsed active={isActive(pathname,"/portfolio",["/accounts","/orders","/incidents"])} />
            <Menu.Root>
              <SidebarHint label={t("sectionAdvanced")}>
                <Menu.Trigger asChild>
                  <button
                    type="button"
                    className={`sidebar-item w-full justify-center px-0 ${moreActive ? "sidebar-item-active" : "sidebar-item-idle"}`}
                    aria-label={t("sectionAdvanced")}
                    aria-current={moreActive ? "page" : undefined}
                  >
                    <MoreIcon size={18} />
                  </button>
                </Menu.Trigger>
              </SidebarHint>
              <Menu.Portal>
                <Menu.Content
                  className={`ui-select-menu ${styles.moreMenu}`}
                  side="right"
                  align="start"
                  sideOffset={10}
                  collisionPadding={8}
                >
                  <Menu.Item asChild>
                    <Link
                      href="/factors"
                      className={`ui-select-option ${styles.moreMenuItem}`}
                      data-current={isActive(pathname, "/factors") || undefined}
                      aria-current={isActive(pathname, "/factors") ? "page" : undefined}
                    >
                      <SkillsIcon size={16} className={styles.moreMenuIcon} />
                      <span className="truncate">{t("factors")}</span>
                    </Link>
                  </Menu.Item>
                  <Menu.Item asChild>
                    <Link
                      href="/workflows"
                      className={`ui-select-option ${styles.moreMenuItem}`}
                      data-current={isActive(pathname, "/workflows") || undefined}
                      aria-current={isActive(pathname, "/workflows") ? "page" : undefined}
                    >
                      <TriggersIcon size={16} className={styles.moreMenuIcon} />
                      <span className="truncate">{t("automation")}</span>
                    </Link>
                  </Menu.Item>
                  {advancedItems.length > 0 ? <Menu.Separator className={styles.moreMenuSeparator} /> : null}
                  {advancedItems.map((item) => {
                    const ItemIcon = resolveNavIcon(item) ?? OverviewIcon;
                    const active = isActive(pathname, item.href, item.match_hrefs);
                    return (
                      <Menu.Item key={item.href} asChild>
                        <Link
                          href={item.href}
                          className={`ui-select-option ${styles.moreMenuItem}`}
                          data-current={active || undefined}
                          aria-current={active ? "page" : undefined}
                          title={item.tagline || undefined}
                        >
                          <ItemIcon size={16} className={styles.moreMenuIcon} />
                          <span className="truncate">{safeNavTranslate(tNav, item.href, item.label)}</span>
                        </Link>
                      </Menu.Item>
                    );
                  })}
                </Menu.Content>
              </Menu.Portal>
            </Menu.Root>
          </div>
          {railCollapsed && <SideRow icon={SearchIcon} label={t("search")} collapsed onClick={() => palette.setOpen(true)} />}
          {!inDrawer && !isNarrow && <div className={railCollapsed ? undefined : styles.collapseControl}><SideRow icon={PanelLeftIcon} label={t(railCollapsed ? "expand" : "collapse")} collapsed onClick={() => setCollapsed(value => !value)} /></div>}
          <SideRow icon={SettingsIcon} label={t("settings")} href="/settings" collapsed active={isActive(pathname,"/settings")} />
        </nav>
        {!railCollapsed && <div className="flex min-w-0 flex-1 flex-col rounded-tl-xl border-l" style={{borderColor:"var(--line)",background:"var(--bg-deep)"}}>
          <div className="flex h-14 shrink-0 items-center gap-2 px-4">
            <Link href="/chat" className="min-w-0 truncate text-[15px] font-semibold text-[color:var(--text-base)]">{t("brandName")}</Link>
            <SidebarHint label={t("search")}><button type="button" className="ui-icon-button ml-auto" aria-label={t("search")} onClick={() => palette.setOpen(true)}><SearchIcon size={16} /></button></SidebarHint>
          </div>
        {/* Recent tasks and strategy folders share one scroll region. */}
        {!railCollapsed ? (
          <div className="flex min-h-0 flex-1 flex-col">
            <div className="embedded-scroll min-h-0 flex-1 px-2 pb-6">
              <AssistantSidebar />
              <SidebarStrategies />
            </div>
          </div>
        ) : null}
        </div>}
      </div>
    </aside>
  );
}

export default CodexSidebar;
