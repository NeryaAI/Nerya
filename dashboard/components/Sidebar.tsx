"use client";

import { AnimatePresence, motion } from "framer-motion";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useMemo, useState } from "react";
import { clientApi } from "../lib/clientApi";
import type { NavEntry, OperatorNavData } from "../lib/operatorTypes";
import { useOperatorNav } from "../lib/useOperatorNav";
import { useUiSettings } from "../lib/settings";
import {
  ChevronLeftIcon,
  ChevronRightIcon,
  MoonIcon,
  NAV_ICONS,
  NAV_ICON_BY_NAME,
  NeryaMark,
} from "./icons";
import { LanguageSwitcher } from "./LanguageSwitcher";
import { SwitchIndicator } from "./SwitchControl";

const COLLAPSE_KEY = "nerya.sidebar.collapsed";
const ADVANCED_OPEN_KEY = "nerya.sidebar.advanced-open";

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

function NavList({
  items,
  pathname,
  collapsed,
  badges,
  tNav,
}: {
  items: NavEntry[];
  pathname: string;
  collapsed: boolean;
  badges?: Record<string, number | undefined>;
  tNav?: (key: string) => string;
}) {
  return (
    <ul className="space-y-0.5">
      {items.map((item) => {
        const active = isActiveNavItem(pathname, item);
        const Icon = resolveIcon(item);
        const badge = badges?.[item.id];
        const translated = tNav ? safeNavTranslate(tNav, item.href, item.label) : item.label;
        return (
          <motion.li key={item.href} whileTap={{ scale: 0.97 }}>
            <Link
              href={item.href}
              title={collapsed ? translated : item.tagline ?? translated}
              className={[
                "group sidebar-item",
                active ? "sidebar-item-active" : "sidebar-item-idle",
                collapsed ? "justify-center px-0" : "",
              ].join(" ")}
            >
              {Icon ? (
                <Icon
                  size={18}
                  className={
                    active
                      ? "text-brand-200"
                      : "text-ink-400 group-hover:text-ink-100"
                  }
                />
              ) : null}
              {!collapsed ? (
                <>
                  <motion.span
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ duration: 0.15, delay: 0.08 }}
                    className="truncate"
                  >
                    {translated}
                  </motion.span>
                  {typeof badge === "number" && badge > 0 ? (
                    <span className="ml-auto inline-flex items-center justify-center min-w-[20px] h-5 px-1.5 rounded-full bg-rose-500/20 text-rose-200 text-[10px] font-mono">
                      {badge > 99 ? "99+" : badge}
                    </span>
                  ) : active ? (
                    <ChevronRightIcon
                      size={14}
                      className="ml-auto text-brand-200"
                    />
                  ) : null}
                </>
              ) : null}
            </Link>
          </motion.li>
        );
      })}
    </ul>
  );
}

function safeNavTranslate(t: (k: string) => string, href: string, fallback: string): string {
  try {
    const v = t(href);
    // next-intl returns "nav.xxx" or the raw key when a translation is missing.
    // Treat both as "no translation available" and fall back to the backend label.
    if (!v) return fallback;
    if (v === href) return fallback;
    if (v.startsWith("nav.")) return fallback;
    return v;
  } catch {
    return fallback;
  }
}

export function Sidebar() {
  const pathname = usePathname();
  const [collapsed, setCollapsed] = useState(false);
  const [hydrated, setHydrated] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [inboxCount, setInboxCount] = useState<number | null>(null);
  const [settings, patchSettings] = useUiSettings();
  const t = useTranslations("sidebar");
  const tNav = useTranslations("nav");

  const nav = useOperatorNav();

  useEffect(() => {
    try {
      const raw = localStorage.getItem(COLLAPSE_KEY);
      if (raw === "1") setCollapsed(true);
      const adv = localStorage.getItem(ADVANCED_OPEN_KEY);
      if (adv === "1") setAdvancedOpen(true);
    } catch {
      // ignore
    }
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? "1" : "0");
    } catch {
      // ignore
    }
  }, [collapsed, hydrated]);

  useEffect(() => {
    if (!hydrated) return;
    try {
      localStorage.setItem(ADVANCED_OPEN_KEY, advancedOpen ? "1" : "0");
    } catch {
      // ignore
    }
  }, [advancedOpen, hydrated]);

  // Poll the inbox so the Action Inbox entry can show its badge.
  // Pulled directly here (not via TopHeader) so collapsed sidebars
  // still render an unread indicator next to the icon.
  useEffect(() => {
    let mounted = true;
    async function tick() {
      try {
        const env = await clientApi.inboxItems({ requires_action: true, limit: 200 });
        if (mounted) setInboxCount(env.data?.needs_action ?? 0);
      } catch {
        if (mounted) setInboxCount(null);
      }
    }
    tick();
    const t = setInterval(tick, 30_000);
    return () => {
      mounted = false;
      clearInterval(t);
    };
  }, []);

  const data: OperatorNavData = nav.data;
  const badges = useMemo(
    () => ({ inbox: inboxCount ?? undefined }),
    [inboxCount],
  );

  const width = collapsed ? "w-[72px]" : "w-64";

  return (
    <aside
      className={`${width} embedded-scroll shrink-0 border-r backdrop-blur-xl h-screen sticky top-0 overflow-x-hidden flex flex-col transition-[width] duration-200`}
      style={{ background: "var(--panel-bg, rgba(4,4,13,0.6))", borderColor: "var(--line)", color: "var(--text-muted)" }}
    >
      <div className="px-4 py-5 border-b flex items-center gap-3" style={{ borderColor: "var(--line)" }}>
        <div className="relative w-10 h-10 rounded-xl bg-gradient-to-br from-brand-500/40 via-brand-600/30 to-fluid-500/20 ring-1 ring-brand-500/40 flex items-center justify-center shadow-glow">
          <NeryaMark size={22} />
          <span className="absolute -inset-px rounded-xl ring-1 ring-white/10 pointer-events-none" />
        </div>
        {!collapsed ? (
          <div className="leading-none">
            <div className="text-white text-[17px] font-semibold tracking-[0.22em]">
              NERYA
            </div>
            <div className="mt-1 text-[9px] uppercase tracking-[0.28em] text-fluid-400/80">
              Evolutionary Brain
            </div>
          </div>
        ) : null}
      </div>

      <AnimatePresence initial={false} mode="wait">
        {!collapsed ? (
          <motion.div
            key="agent-expanded"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="mx-3 mt-4 glass px-3 py-3"
          >
          <div className="flex items-center gap-3">
            <div className="relative shrink-0">
              <div className="absolute inset-0 rounded-full ring-ai animate-spin-slow opacity-70" style={{ animation: "aurora-shift 8s linear infinite" }} />
              <div className="relative w-10 h-10 rounded-full bg-gradient-to-br from-brand-400 via-brand-500 to-brand-700 flex items-center justify-center text-white font-bold text-sm shadow-glow">
                N
              </div>
              <span className="absolute -bottom-0.5 -right-0.5 w-3 h-3 rounded-full bg-accent-500 ring-2 ring-[#04040d]" />
            </div>
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <span className="text-[12px] font-semibold text-white tracking-wider">
                  NERYA AGENT
                </span>
                <span className="text-[9px] font-mono text-brand-200 bg-brand-500/20 border border-brand-500/30 rounded px-1 py-[1px]">
                  v0.1.0
                </span>
              </div>
              <div className="text-[10px] text-ink-400 mt-0.5 truncate">
                Self-evolving runtime
              </div>
              <div className="flex items-center gap-1 mt-1">
                <span className="w-1.5 h-1.5 rounded-full bg-accent-500 shadow-neon animate-pulse" />
                <span className="text-[9px] text-accent-400 tracking-widest font-mono uppercase">
                  ONLINE
                </span>
              </div>
            </div>
          </div>
          </motion.div>
        ) : (
          <motion.div
            key="agent-collapsed"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="flex justify-center mt-4"
          >
          <div className="relative">
            <div className="w-9 h-9 rounded-full bg-gradient-to-br from-brand-400 via-brand-500 to-brand-700 flex items-center justify-center text-white font-bold text-sm shadow-glow">
              N
            </div>
            <span className="absolute -bottom-0.5 -right-0.5 w-2.5 h-2.5 rounded-full bg-accent-500 ring-2 ring-[#0a0b1a]" />
          </div>
          </motion.div>
        )}
      </AnimatePresence>

      <nav className="px-3 py-5 space-y-5 flex-1">
        <div>
          {!collapsed ? (
            <div className="px-2 mb-2 text-[10px] font-semibold uppercase tracking-[0.2em] text-ink-500">
              {t("sectionOperate")}
            </div>
          ) : (
            <div className="mx-3 h-px bg-brand-500/10 mb-3" />
          )}
          <NavList
            items={data.primary}
            pathname={pathname}
            collapsed={collapsed}
            badges={badges}
            tNav={tNav}
          />
        </div>

        {data.advanced.length > 0 ? (
          <div>
            {!collapsed ? (
              <button
                type="button"
                onClick={() => setAdvancedOpen((v) => !v)}
                className="w-full flex items-center justify-between px-2 mb-2 text-[10px] font-semibold uppercase tracking-[0.2em] text-ink-500 hover:text-ink-200"
              >
                <span>{t("sectionAdvanced")}</span>
                <span className="text-[10px]">{advancedOpen ? "−" : "+"}</span>
              </button>
            ) : (
              <div className="mx-3 h-px bg-brand-500/10 mb-3" />
            )}
            {(advancedOpen || collapsed) && (
              <NavList
                items={data.advanced}
                pathname={pathname}
                collapsed={collapsed}
                tNav={tNav}
              />
            )}
          </div>
        ) : null}

        {!collapsed && data.hidden.length > 0 ? (
          <div className="px-2 text-[10px] text-ink-500/70">
            <div className="font-semibold uppercase tracking-[0.2em] text-ink-500 mb-1">
              {t("sectionHidden")}
            </div>
            <ul className="space-y-1">
              {data.hidden.slice(0, 5).map((h) => (
                <li
                  key={h.id}
                  className="text-[10px] leading-snug"
                  title={h.reason}
                >
                  <span className="text-ink-400">{h.label}</span>
                  <span className="text-ink-500"> · {h.reason}</span>
                  {h.fix_action?.href ? (
                    <Link
                      href={h.fix_action.href}
                      className="ml-1 text-brand-300 hover:underline"
                    >
                      {t("fix")}
                    </Link>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </nav>

      <div className="border-t border-brand-500/10 p-3 space-y-2">
        {!collapsed ? (
          <LanguageSwitcher />
        ) : (
          <LanguageSwitcher collapsed />
        )}
        <button
          onClick={() => patchSettings({ darkMode: !settings.darkMode })}
          className={`w-full flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-ink-300 hover:bg-brand-500/10 hover:text-white transition-colors ${
            collapsed ? "justify-center px-0" : ""
          }`}
          title={collapsed ? (settings.darkMode ? t("lightMode") : t("darkMode")) : undefined}
        >
          <MoonIcon size={18} />
          {!collapsed ? <span>{settings.darkMode ? t("darkMode") : t("lightMode")}</span> : null}
          {!collapsed ? (
            <span className="ml-auto">
              <SwitchIndicator checked={settings.darkMode} label="Dark mode" tone="brand" size="sm" />
            </span>
          ) : null}
        </button>
        <button
          onClick={() => setCollapsed((v) => !v)}
          className={`w-full flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-ink-300 hover:bg-brand-500/10 hover:text-white transition-colors ${
            collapsed ? "justify-center px-0" : ""
          }`}
          title={collapsed ? t("expand") : t("collapse")}
        >
          {collapsed ? (
            <ChevronRightIcon size={18} />
          ) : (
            <ChevronLeftIcon size={18} />
          )}
          {!collapsed ? <span>{t("collapse")}</span> : null}
        </button>
      </div>
    </aside>
  );
}
