"use client";

/**
 * /settings — full Settings workspace.
 *
 * The actual implementation lives in `components/SettingsWorkspace.tsx`
 * so the Memory-only mount at `/memory/page.tsx` can share the same
 * source of truth (state hooks, helper functions, JSX) without
 * duplicating ~7000 lines of code. Memory is no longer part of the
 * Settings section nav — it has its own page under the top-bar "More"
 * menu.
 */

import { SettingsWorkspace } from "../../components/SettingsWorkspace";

export default function SettingsPage() {
  // Section navigation lives in the Codex-style SettingsSidebar (the
  // left rail takeover wired up in AppShell), so the page body renders
  // just the active panel at full width.
  return <SettingsWorkspace hideSectionNav />;
}
