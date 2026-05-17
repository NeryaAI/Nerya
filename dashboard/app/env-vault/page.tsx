"use client";

/**
 * Standalone Env & Vault page.
 *
 * Mounts the shared SettingsWorkspace component with
 * `forceSection="envvault"` so the operator gets the full Runtime
 * environment + SecretVault references UI on a dedicated page under
 * the top-bar "More" menu, without duplicating state hooks or JSX
 * between `/settings` and `/env-vault`.
 *
 * The Env + Vault cards were extracted out of the Settings → Network
 * & Env tab; that tab now only owns proxy + remote tunnels + runtime
 * feature flags. Anyone with a stale `/settings#envvault` deep-link
 * is redirected here by the Settings workspace's hash handler.
 */

import { SettingsWorkspace } from "../../components/SettingsWorkspace";

export default function EnvVaultPage() {
  return <SettingsWorkspace forceSection="envvault" />;
}
