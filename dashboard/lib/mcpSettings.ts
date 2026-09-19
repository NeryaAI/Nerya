import { authHeaders } from "./auth";

export type McpStatus = {
  ok: boolean; revision: string; enabled: boolean; auth_mode: string;
  sdk_installed: boolean; admin_password_configured: boolean;
  public_url: string; endpoint: string; oauth_issuer: string; detected_public_urls: string[];
  allow_mutating: boolean; native_allow_mutating: boolean; allow_tools: string[] | null; deny_tools: string[];
  openai_tunnel: { enabled: boolean; tunnel_id: string; installed: boolean; api_key_configured: boolean;
    running: boolean; ready: boolean; error: string; install_command: string; api_tool: Record<string, string> };
};
export type SkillRow = { id: string; description: string; source: string; revision: string;
  entry: string; enabled: boolean; assigned: boolean | null; edit_effect: string };
export type SkillCatalog = { skills: SkillRow[]; total: number; next_offset: number | null;
  binding_revision: string; enabled_revision: string };
export type SkillFile = { id: string; source: string; file: string; revision: string; text: string;
  total_chars: number; next_offset: number | null; files: string[] };

export async function mcpRequest<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch("/api/proxy" + path, { method: body === undefined ? "GET" : "POST",
    headers: authHeaders(body === undefined ? undefined : { "Content-Type": "application/json" }),
    body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", signal });
  const value = await response.json();
  if (!response.ok || value.ok === false || value.error) {
    const message = typeof value.error === "string" ? value.error : value.error?.message;
    throw new Error(message || `Request failed (${response.status})`);
  }
  return value as T;
}
