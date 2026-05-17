"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";

type SectionKey = "trading" | "strategy" | "runtime";

type TabItem = {
  labelKey: string;
  href: string;
  match?: string[];
};

const SECTIONS: Record<
  SectionKey,
  {
    labelKey: string;
    tabs: TabItem[];
  }
> = {
  trading: {
    labelKey: "trading",
    tabs: [
      { labelKey: "portfolio", href: "/portfolio" },
      { labelKey: "accounts", href: "/accounts", match: ["/accounts"] },
      { labelKey: "orders", href: "/orders" },
      { labelKey: "incidents", href: "/incidents" },
    ],
  },
  strategy: {
    labelKey: "strategyLab",
    tabs: [
      { labelKey: "strategies", href: "/strategies", match: ["/strategies"] },
      { labelKey: "workflows", href: "/workflows" },
    ],
  },
  runtime: {
    labelKey: "runtimeLibrary",
    tabs: [
      { labelKey: "agents", href: "/agents" },
      { labelKey: "skills", href: "/skills" },
      { labelKey: "tasks", href: "/tasks" },
    ],
  },
};

function pathMatches(pathname: string, href: string): boolean {
  return pathname === href || pathname.startsWith(`${href}/`);
}

function isActive(pathname: string, tab: TabItem): boolean {
  const candidates = [tab.href, ...(tab.match ?? [])];
  return candidates.some((href) => pathMatches(pathname, href));
}

export function SectionTabs({ section }: { section: SectionKey }) {
  const t = useTranslations("sectionTabs");
  const pathname = usePathname() || "";
  const config = SECTIONS[section];

  return (
    <nav
      aria-label={t("tabsAriaLabel", { section: t(config.labelKey) })}
      className="mb-5 -mt-3 overflow-x-auto pb-1"
    >
      <div className="inline-flex min-w-full items-center gap-1 border-b border-brand-500/10">
        {config.tabs.map((tab) => {
          const active = isActive(pathname, tab);
          return (
            <Link
              key={tab.href}
              href={tab.href}
              aria-current={active ? "page" : undefined}
              className={[
                "relative shrink-0 px-3 py-2 text-[12px] font-medium transition-colors",
                active
                  ? "text-white"
                  : "text-ink-400 hover:text-ink-100",
              ].join(" ")}
            >
              {t(tab.labelKey)}
              {active ? (
                <span className="absolute inset-x-2 -bottom-px h-px bg-brand-300 shadow-[0_0_10px_rgba(180,139,255,0.75)]" />
              ) : null}
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
