"use client";

/**
 * Browsers workspace. Two-tab self-contained page that replaces both the
 * old `/browsers` (engine inventory) and `/browser-session` (interactive
 * driver) routes.
 *
 *  • Engines  — install / select / configure headless browser engines
 *               (camofox, cloakbrowser, lightpanda, obscura, etc.).
 *               Currently still mounts the shared SettingsWorkspace
 *               `browsers` panel (the install/select UI is identical to
 *               what Settings used to host); this avoids duplicating that
 *               very specific install machinery while we redesign other
 *               surfaces.
 *  • Session  — interactive CDP-driven browser session (mounts
 *               `BrowserSessionPanel`). All the legacy fetch-mode
 *               render-mode toggles and the standalone JS-eval input are
 *               gone; the panel exposes the basic mouse/keyboard
 *               operations the user actually needs (click x/y, click
 *               selector, scroll, type, press key) plus a Console viewer
 *               and a Network requests viewer fed by the existing
 *               `get_console` / `get_network` CDP actions.
 *
 * Tab is persisted in the URL via `?tab=session|engines` and reflected in
 * the browser History API (so the More menu deep-link and the legacy
 * `/browser-session` redirect both land on the correct sub-tab).
 *
 * Errors anywhere in this workspace surface via `dialogs.toast()`; we
 * never display a banner at the top of the page while the form lives at
 * the bottom (the workflow-vs-feedback proximity problem the operator
 * called out).
 */

import { useTranslations } from "next-intl";
import { useEffect, useState } from "react";

import { PageBody, PageHeader } from "../../components/Page";
import { SettingsWorkspace } from "../../components/SettingsWorkspace";
import { BrowserSessionPanel } from "../../components/BrowserSessionPanel";

type SubTab = "engines" | "session";

function pickInitialTab(): SubTab {
  if (typeof window === "undefined") return "engines";
  const params = new URLSearchParams(window.location.search);
  const raw = (params.get("tab") || "").toLowerCase();
  if (raw === "session" || raw === "engines") return raw;
  // legacy hash deep-links (#session) still work
  const hash = (window.location.hash || "").replace(/^#/, "").toLowerCase();
  if (hash === "session") return "session";
  return "engines";
}

export default function BrowsersPage() {
  const t = useTranslations("browsersPage");
  const [tab, setTab] = useState<SubTab>("engines");

  // Initialise from URL after mount (avoids hydration mismatch). We
  // deliberately do NOT keep tab→URL synced via useEffect — that
  // raced with the legacy /browser-session redirect and silently
  // stripped the inbound `?tab=session` query.
  useEffect(() => {
    setTab(pickInitialTab());
  }, []);

  /** Switch tab + reflect the choice in the URL (no scroll, no nav). */
  const selectTab = (next: SubTab) => {
    setTab(next);
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (next === "engines") {
      url.searchParams.delete("tab");
    } else {
      url.searchParams.set("tab", next);
    }
    window.history.replaceState({}, "", url.toString());
  };

  const tabs: { id: SubTab; label: string }[] = [
    { id: "engines", label: t("tabEngines") },
    { id: "session", label: t("tabSession") },
  ];

  // When the "Engines" tab is active we hand off the entire layout to the
  // shared SettingsWorkspace (it owns its own PageHeader / chrome) so we
  // skip the workspace header and tab strip below. This avoids a confusing
  // double-header. For the Session tab we render our own workspace
  // chrome.
  if (tab === "engines") {
    return (
      <SettingsWorkspace
        forceSection="browsers"
        topBanner={
          <div className="mb-3 flex flex-wrap gap-2">
            {tabs.map((entry) => (
              <button
                key={entry.id}
                type="button"
                onClick={() => setTab(entry.id)}
                className={`rounded-full border px-3 py-1 text-[11px] transition-colors ${
                  tab === entry.id
                    ? "border-brand-500/50 bg-brand-500/15 text-brand-100"
                    : "border-brand-500/15 bg-ink-950/40 text-ink-300 hover:border-brand-500/30 hover:text-ink-100"
                }`}
              >
                {entry.label}
              </button>
            ))}
          </div>
        }
      />
    );
  }

  return (
    <PageBody>
      <PageHeader
        eyebrow={t("eyebrow")}
        title={t("title")}
        description={t("description")}
      />

      <div className="flex flex-wrap gap-2">
        {tabs.map((entry) => (
          <button
            key={entry.id}
            type="button"
            onClick={() => setTab(entry.id)}
            className={`rounded-full border px-3 py-1 text-[11px] transition-colors ${
              tab === entry.id
                ? "border-brand-500/50 bg-brand-500/15 text-brand-100"
                : "border-brand-500/15 bg-ink-950/40 text-ink-300 hover:border-brand-500/30 hover:text-ink-100"
            }`}
          >
            {entry.label}
          </button>
        ))}
      </div>

      <BrowserSessionPanel />
    </PageBody>
  );
}
