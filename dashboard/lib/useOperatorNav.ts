"use client";

import { useEffect, useState } from "react";
import { clientApi } from "./clientApi";
import type { OperatorNavData } from "./operatorTypes";

/**
 * Capability-aware navigation hook.
 *
 * Calls ``GET /operator/nav`` and exposes:
 *
 * * ``data``     — the latest envelope payload (or ``null`` while loading).
 * * ``loading``  — first-fetch indicator.
 * * ``error``    — most recent error message, if any.
 * * ``refresh()`` — re-fetch (used after a setup-readiness fix changes
 *                  capability flags).
 *
 * Falls back to a static "always visible" set (Home, Agent Workspace,
 * Action Inbox, Settings) when the endpoint can't be reached so the
 * user still has a reachable surface.
 */

const FALLBACK: OperatorNavData = {
  primary: [
    {
      id: "home",
      label: "Home",
      href: "/dashboard",
      icon: "home",
      always_visible: true,
    },
    {
      id: "agent_workspace",
      label: "Agent Workspace",
      href: "/chat",
      icon: "chat",
      always_visible: true,
    },
    {
      id: "trading",
      label: "Trading",
      href: "/portfolio",
      match_hrefs: ["/accounts", "/orders", "/incidents"],
      icon: "portfolio",
      always_visible: true,
    },
    {
      id: "strategy_lab",
      label: "Strategy Lab",
      href: "/strategies",
      match_hrefs: ["/workflows"],
      icon: "strategy",
      always_visible: true,
    },
    {
      id: "runtime_library",
      label: "Runtime Library",
      href: "/agents",
      match_hrefs: ["/skills", "/tasks"],
      icon: "agents",
      always_visible: true,
    },
    {
      id: "inbox",
      label: "Action Inbox",
      href: "/inbox",
      icon: "inbox",
      always_visible: true,
    },
    {
      id: "settings",
      label: "Settings",
      href: "/settings",
      icon: "settings",
      always_visible: true,
    },
  ],
  advanced: [],
  hidden: [],
  capabilities: {},
};

export function useOperatorNav(intervalMs = 60_000) {
  const [data, setData] = useState<OperatorNavData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function fetchOnce() {
    try {
      const env = await clientApi.operatorNav();
      setData(env.data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setData((prev) => prev ?? FALLBACK);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchOnce();
    if (!intervalMs) return;
    const t = setInterval(fetchOnce, intervalMs);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs]);

  return {
    data: data ?? FALLBACK,
    loading,
    error,
    refresh: fetchOnce,
  };
}
