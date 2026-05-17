"use client";

/**
 * Standalone Web Search page.
 *
 * Mounts the shared SettingsWorkspace component with
 * `forceSection="search"` so the operator gets the full multi-engine
 * web-search chain + per-engine API key rotation UI on a dedicated
 * page under the top-bar "More" menu, without duplicating state hooks
 * or JSX between `/settings` and `/web-search`.
 *
 * The Web search section has been removed from the regular Settings
 * page (section nav + panel). Any legacy `/settings#search` deep-link
 * is redirected to `/web-search` by the Settings workspace's hash
 * handler.
 */

import { SettingsWorkspace } from "../../components/SettingsWorkspace";

export default function WebSearchPage() {
  return <SettingsWorkspace forceSection="search" />;
}
