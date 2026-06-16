"use client";

/**
 * ShellNotifications — the top-right notification bell.
 *
 * Codex keeps notifications in the top-right of the window rather than in
 * the left rail. This floats over the main content area (top-right) and
 * links to the Inbox, showing the needs-action count as a badge. It polls
 * the same inbox endpoint the rail used to.
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";
import { clientApi } from "../../lib/clientApi";
import { BellIcon } from "../icons";

export function ShellNotifications() {
  const t = useTranslations("topHeader");
  const pathname = usePathname() || "/";
  const [count, setCount] = useState(0);

  useEffect(() => {
    let mounted = true;
    async function tick() {
      try {
        const env = await clientApi.inboxItems({ requires_action: true, limit: 200 });
        if (mounted) setCount(env.data?.needs_action ?? 0);
      } catch {
        /* keep last known count on transient failures */
      }
    }
    tick();
    const timer = setInterval(tick, 30_000);
    return () => {
      mounted = false;
      clearInterval(timer);
    };
  }, []);

  const active = pathname === "/inbox" || pathname.startsWith("/inbox/");

  return (
    <Link
      href="/inbox"
      className={`relative inline-flex h-9 w-9 items-center justify-center rounded-lg border backdrop-blur-glass transition-colors ${
        active
          ? "border-brand-500/45 text-brand-200"
          : "text-[color:var(--text-muted)] hover:text-[color:var(--text-base)] hover:border-brand-500/30"
      }`}
      style={{
        borderColor: active ? undefined : "var(--line)",
        background: "var(--panel-bg)",
      }}
      title={t("actionInbox")}
      aria-label={t("actionInbox")}
    >
      <BellIcon size={17} />
      {count > 0 ? (
        <span className="absolute -right-1 -top-1 inline-flex h-4 min-w-[16px] items-center justify-center rounded-full bg-rose-500 px-1 text-[10px] font-medium leading-none text-white">
          {count > 99 ? "99+" : count}
        </span>
      ) : null}
    </Link>
  );
}

export default ShellNotifications;
