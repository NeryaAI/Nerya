"use client";

/**
 * Standalone Memory workspace page.
 *
 * Mounts the shared SettingsWorkspace component with
 * `forceSection="memory"` so the operator gets the full Memory UI
 * (backend chooser, per-backend full config, notebook, activity log,
 * write rules, providers, evidence vault, operator profile) on a
 * dedicated page under the top-bar "More" menu — without duplicating
 * state hooks or JSX between `/settings` and `/memory`.
 *
 * The Memory section has been removed from the regular Settings page
 * (section nav + panel). Any legacy `/settings#memory` deep-link is
 * redirected to `/memory` by the Settings workspace's hash handler.
 */

import { SettingsWorkspace } from "../../components/SettingsWorkspace";

export default function MemoryPage() {
  return <SettingsWorkspace forceSection="memory" />;
}
