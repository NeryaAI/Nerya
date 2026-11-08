import { authHeaders, handleAuthFailure } from "./auth";

export type McpStatus = {
  ok: boolean; revision: string; enabled: boolean; auth_mode: string;
  sdk_installed: boolean; admin_password_configured: boolean;
  public_url: string; endpoint: string; oauth_issuer: string; detected_public_urls: string[];
  trust_mode: "workspace";
  openai_tunnel: { enabled: boolean; tunnel_id: string; installed: boolean; api_key_configured: boolean;
    running: boolean; ready: boolean; error: string; install_command: string; api_tool: Record<string, string>;
    installing: boolean; install_supported: boolean; install_error: string };
};

/** Polling must never replace unsaved connection fields or clear an entered key. */
export function mergeTunnelRuntime(status: McpStatus, live: Partial<McpStatus["openai_tunnel"]>): McpStatus {
  const keys = ["installed", "installing", "install_supported", "install_error", "install_command", "running", "ready", "error"] as const;
  const runtime = Object.fromEntries(keys.filter(key => key in live).map(key => [key, live[key]]));
  return { ...status, openai_tunnel: { ...status.openai_tunnel, ...runtime } };
}
export type SkillRow = { id: string; title?: string; description: string; source: string; revision: string;
  entry: string; enabled: boolean; assigned: boolean | null; edit_effect: string;
  catalog_parent?: string; catalog_group?: "core" | "professional"; method_count?: number };
export type SkillCatalog = { skills: SkillRow[]; total: number; next_offset: number | null;
  binding_revision: string; enabled_revision: string };
export type SkillFile = { id: string; source: string; file: string; revision: string; text: string;
  total_chars: number; next_offset: number | null; files: string[] };

export async function mcpRequest<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch("/api/proxy" + path, { method: body === undefined ? "GET" : "POST",
    headers: authHeaders(body === undefined ? undefined : { "Content-Type": "application/json" }),
    body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", signal });
  const value = await response.json();
  if (!response.ok) handleAuthFailure(response.status, JSON.stringify(value));
  if (!response.ok || value.ok === false || value.error) {
    const message = typeof value.error === "string" ? value.error : value.error?.message;
    throw new Error(message || `Request failed (${response.status})`);
  }
  return value as T;
}
