"use client";

/**
 * /setup — unified onboarding wizard.
 *
 * Same code path is reachable from `nerya setup --web` (CLI opens this
 * URL in the user's default browser) and from the dashboard's top
 * navigation. The wizard reuses `SettingsWorkspace` cards via the
 * `forceSection` prop so no UI logic is duplicated between
 * /settings and /setup.
 */

import { SetupWizard } from "../../components/SetupWizard";

export default function SetupPage() {
  return <SetupWizard />;
}
