"use client";

import { motion } from "framer-motion";
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
  NAV_ICONS,
  NAV_ICON_BY_NAME,
} from "./icons";
import { LanguageSwitcher } from "./LanguageSwitcher";
import { NeryaLogo } from "./NeryaLogo";

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
                    <span className="ml-auto inline-flex items-center justify-center min-w-[20px] h-5 px-1.5 rounded-full bg-rose-500/15 text-rose-400 text-[11px]">
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
  const pathname = usePathname() || "/dashboard";
  const [collapsed, setCollapsed] = useState(false);
  const [hydrated, setHydrated] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [inboxCount, setInboxCount] = useState<number | null>(null);
  const [browserSessionCount, setBrowserSessionCount] = useState<number | null>(null);
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

  // Poll the live browser-session count so the Advanced → Browser session
  // entry can show how many sessions are currently held by the engine.
  useEffect(() => {
    let mounted = true;
    async function tick() {
      try {
        const res = await clientApi.browserSessionList();
        if (mounted) setBrowserSessionCount(res.count ?? 0);
      } catch {
        if (mounted) setBrowserSessionCount(null);
      }
    }
    tick();
    const t = setInterval(tick, 15_000);
    return () => {
      mounted = false;
      clearInterval(t);
    };
  }, []);

  const data: OperatorNavData = nav.data;
  const badges = useMemo(
    () => ({
      inbox: inboxCount ?? undefined,
      browser_session: browserSessionCount ?? undefined,
    }),
    [inboxCount, browserSessionCount],
  );

  const width = collapsed ? "w-[72px]" : "w-64";

  return (
    <aside
      className={`${width} embedded-scroll shrink-0 border-r backdrop-blur-xl h-screen sticky top-0 overflow-x-hidden flex flex-col transition-[width] duration-200`}
      style={{ background: "var(--panel-bg, rgba(4,4,13,0.6))", borderColor: "var(--line)", color: "var(--text-muted)" }}
    >
      <div
        className="px-4 py-4 border-b flex items-center gap-3"
        style={{ borderColor: "var(--line)" }}
      >
        <Link
          href="/dashboard"
          className="flex items-center gap-3 min-w-0 group"
          aria-label="Nerya"
        >
          <div className="relative w-9 h-9 rounded-lg overflow-hidden bg-black/30 flex items-center justify-center">
            <NeryaLogo size={36} />
          </div>
          {!collapsed ? (
            <span className="text-[16px] font-medium text-[color:var(--text-base)] group-hover:text-brand-300 transition-colors">
              {t("brandName")}
            </span>
          ) : null}
        </Link>
      </div>

      <nav className="px-3 py-4 space-y-5 flex-1">
        <div>
          {!collapsed ? (
            <div className="px-2 mb-1.5 text-[12px] font-medium text-[color:var(--text-muted)]">
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
                className="w-full flex items-center justify-between px-2 mb-1.5 text-[12px] font-medium text-[color:var(--text-muted)] hover:text-[color:var(--text-base)]"
              >
                <span>{t("sectionAdvanced")}</span>
                <span className="text-[12px]">{advancedOpen ? "−" : "+"}</span>
              </button>
            ) : (
              <div className="mx-3 h-px bg-brand-500/10 mb-3" />
            )}
            {(advancedOpen || collapsed) && (
              <NavList
                items={data.advanced}
                pathname={pathname}
                collapsed={collapsed}
                badges={badges}
                tNav={tNav}
              />
            )}
          </div>
        ) : null}

        {!collapsed && data.hidden.length > 0 ? (
          <div className="px-2">
            <div className="mb-1.5 text-[12px] font-medium text-[color:var(--text-muted)]">
              {t("sectionHidden")}
            </div>
            <ul className="space-y-1">
              {data.hidden.slice(0, 5).map((h) => (
                <li
                  key={h.id}
                  className="text-[12px] leading-snug text-[color:var(--text-muted)]"
                  title={h.reason}
                >
                  <span>{h.label}</span>
                  <span className="opacity-70"> · {h.reason}</span>
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

      <div className="border-t border-brand-500/10 p-3 space-y-1">
        {!collapsed ? (
          <LanguageSwitcher />
        ) : (
          <LanguageSwitcher collapsed />
        )}
        <select
          value={settings.darkMode}
          onChange={(e) => {
            const mode = e.currentTarget.value;
            patchSettings({ darkMode: mode as "light" | "dark" | "system" });
          }}
          className={`w-full rounded-lg px-3 py-2 text-[13px] text-[color:var(--text-muted)] bg-transparent border-0 outline-none hover:bg-brand-500/10 hover:text-[color:var(--text-base)] transition-colors`}
          aria-label={t("systemMode")}
        >
          <option value="system">{t("systemMode")}</option>
          <option value="light">{t("lightMode")}</option>
          <option value="dark">{t("darkMode")}</option>
        </select>
        <button
          onClick={() => setCollapsed((v) => !v)}
          className={`w-full flex items-center gap-3 rounded-lg px-3 py-2 text-[13px] text-[color:var(--text-muted)] hover:bg-brand-500/10 hover:text-[color:var(--text-base)] transition-colors ${
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
