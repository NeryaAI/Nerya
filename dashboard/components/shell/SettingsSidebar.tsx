"use client";

/**
 * SettingsSidebar — the Codex-style settings takeover rail.
 *
 * When the app is on a settings surface (`/settings` and the standalone
 * "More" pages that are really settings sections), the AppShell swaps
 * the main CodexSidebar for this rail so the whole left column becomes
 * the settings navigation — mirroring the Codex desktop app:
 *
 *   ┌──────────────┐
 *   │ ‹ Back to app │
 *   │ Settings      │
 *   │ ⌕ Search…     │
 *   │ GENERAL       │  ── /settings#<hash> sections
 *   │ ✦ Models      │
 *   │ ⛨ Access      │
 *   │ ◍ Network&Env │
 *   │ ⚙ Cap. gates  │
 *   │ ▣ Interface   │
 *   │ INTEGRATIONS  │  ── standalone /routes (forceSection pages)
 *   │ ⌕ Web search  │
 *   │ ◍ Browsers    │
 *   │ ⊙ Memory      │
 *   │ ▤ Env & Vault │
 *   │ ✉ Gateway     │
 *   └──────────────┘
 *
 * The "General" rows drive the in-page section via the URL hash
 * (`/settings#models`), which SettingsWorkspace already listens to via
 * its `hashchange` handler. The "Integrations" rows are ordinary routes.
 */

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useState, type ComponentType, type SVGProps } from "react";
import {
  ChartIcon,
  ChevronLeftIcon,
  FolderIcon,
  GlobeIcon,
  MemoryIcon,
  MessagesIcon,
  SearchIcon,
  ShieldCheckIcon,
  SparkIcon,
  WrenchIcon,
} from "../icons";

type IconComp = ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;

const DEFAULT_HASH = "models";

function pathMatches(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="px-3 pb-1 pt-1 text-[11px] font-medium uppercase tracking-wide text-[color:var(--text-muted)]">
      {children}
    </div>
  );
}

function Row({
  icon: Icon,
  label,
  active,
  href,
  onClick,
}: {
  icon: IconComp;
  label: string;
  active?: boolean;
  href?: string;
  onClick?: () => void;
}) {
  const cls = [
    "group sidebar-item w-full",
    active ? "sidebar-item-active" : "sidebar-item-idle",
  ].join(" ");
  const inner = (
    <>
      <Icon
        size={16}
        className={`shrink-0 ${active ? "text-brand-200" : "text-[color:var(--text-muted)] group-hover:text-[color:var(--text-base)]"}`}
      />
      <span className="truncate">{label}</span>
    </>
  );
  if (href) {
    return (
      <Link href={href} className={cls}>
        {inner}
      </Link>
    );
  }
  return (
    <button type="button" onClick={onClick} className={cls}>
      {inner}
    </button>
  );
}

export function SettingsSidebar() {
  const pathname = usePathname() || "/settings";
  const router = useRouter();
  const tNav = useTranslations("settingsNav");
  const tTabs = useTranslations("settings.tabs");
  const [hash, setHash] = useState(DEFAULT_HASH);
  const [query, setQuery] = useState("");

  const onSettings = pathMatches(pathname, "/settings");

  // Track the active `/settings#<section>` hash so the matching General
  // row highlights. SettingsWorkspace owns the same hash; we just mirror
  // it here for selection state.
  useEffect(() => {
    const read = () =>
      setHash(window.location.hash.replace(/^#/, "") || DEFAULT_HASH);
    read();
    window.addEventListener("hashchange", read);
    return () => window.removeEventListener("hashchange", read);
  }, [pathname]);

  const general: { key: string; label: string; icon: IconComp }[] = [
    { key: "models", label: tTabs("models"), icon: SparkIcon },
    { key: "access", label: tTabs("access"), icon: ShieldCheckIcon },
    { key: "runtime", label: tTabs("runtime"), icon: GlobeIcon },
    { key: "capabilityGates", label: tTabs("capabilityGates"), icon: WrenchIcon },
    { key: "interface", label: tTabs("interface"), icon: ChartIcon },
  ];
  const integrations: { href: string; label: string; icon: IconComp }[] = [
    { href: "/web-search", label: tTabs("search"), icon: SearchIcon },
    { href: "/browsers", label: tTabs("browsers"), icon: GlobeIcon },
    { href: "/memory", label: tTabs("memory"), icon: MemoryIcon },
    { href: "/env-vault", label: tNav("envVault"), icon: FolderIcon },
    { href: "/gateway", label: tNav("gateway"), icon: MessagesIcon },
  ];

  function goHash(key: string) {
    if (!onSettings) {
      router.push(`/settings#${key}`);
      return;
    }
    if (window.location.hash.replace(/^#/, "") !== key) {
      // Fires `hashchange`, which SettingsWorkspace consumes to switch
      // the active panel.
      window.location.hash = key;
    }
    setHash(key);
  }

  const needle = query.trim().toLowerCase();
  const matches = (label: string) => !needle || label.toLowerCase().includes(needle);
  const generalShown = general.filter((i) => matches(i.label));
  const integrationsShown = integrations.filter((i) => matches(i.label));

  return (
    <aside
      className="w-64 nerya-sidebar embedded-scroll sticky top-0 flex h-screen shrink-0 flex-col overflow-x-hidden border-r"
      style={{ background: "var(--panel-bg)", borderColor: "var(--line)" }}
    >
      <div className="px-2 pt-3">
        <Link href="/" className="group sidebar-item sidebar-item-idle w-full">
          <ChevronLeftIcon
            size={16}
            className="shrink-0 text-[color:var(--text-muted)] group-hover:text-[color:var(--text-base)]"
          />
          <span className="truncate">{tNav("backToApp")}</span>
        </Link>
      </div>

      <div className="px-3 pb-1 pt-3">
        <h2 className="text-[14px] font-semibold text-[color:var(--text-base)]">
          {tNav("title")}
        </h2>
      </div>

      <div className="px-3 pb-2 pt-1">
        <div
          className="flex items-center gap-2 rounded-lg border px-2.5 py-1.5 focus-within:border-brand-500/55"
          style={{ borderColor: "var(--line)" }}
        >
          <SearchIcon size={14} className="shrink-0 text-[color:var(--text-muted)]" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={tNav("searchPlaceholder")}
            className="w-full bg-transparent text-[13px] text-[color:var(--text-base)] placeholder:text-[color:var(--text-muted)] focus:outline-none"
          />
        </div>
      </div>

      <nav className="flex-1 space-y-3 overflow-y-auto px-2 pb-2">
        {generalShown.length ? (
          <div className="space-y-0.5">
            <SectionLabel>{tNav("groupGeneral")}</SectionLabel>
            {generalShown.map((item) => (
              <Row
                key={item.key}
                icon={item.icon}
                label={item.label}
                active={onSettings && hash === item.key}
                onClick={() => goHash(item.key)}
              />
            ))}
          </div>
        ) : null}

        {integrationsShown.length ? (
          <div className="space-y-0.5">
            <SectionLabel>{tNav("groupIntegrations")}</SectionLabel>
            {integrationsShown.map((item) => (
              <Row
                key={item.href}
                icon={item.icon}
                label={item.label}
                active={pathMatches(pathname, item.href)}
                href={item.href}
              />
            ))}
          </div>
        ) : null}
      </nav>
    </aside>
  );
}

export default SettingsSidebar;
