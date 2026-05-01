"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

type SectionKey = "trading" | "strategy" | "runtime";

type TabItem = {
  label: string;
  href: string;
  match?: string[];
};

const SECTIONS: Record<
  SectionKey,
  {
    label: string;
    tabs: TabItem[];
  }
> = {
  trading: {
    label: "Trading",
    tabs: [
      { label: "Portfolio", href: "/portfolio" },
      { label: "Accounts", href: "/accounts", match: ["/accounts"] },
      { label: "Orders", href: "/orders" },
      { label: "Incidents", href: "/incidents" },
    ],
  },
  strategy: {
    label: "Strategy Lab",
    tabs: [
      { label: "Strategies", href: "/strategies", match: ["/strategies"] },
      { label: "Workflows", href: "/workflows" },
    ],
  },
  runtime: {
    label: "Runtime Library",
    tabs: [
      { label: "Agents", href: "/agents" },
      { label: "Skills", href: "/skills" },
      { label: "Tasks", href: "/tasks" },
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
  const pathname = usePathname() || "";
  const config = SECTIONS[section];

  return (
    <nav
      aria-label={`${config.label} tabs`}
      className="mb-5 -mt-3 overflow-x-auto pb-1"
    >
      <div className="inline-flex min-w-full items-center gap-1 border-b border-white/5">
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
              {tab.label}
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
