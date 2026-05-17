export const AUTH_TOKEN_KEY = "nerya.admin_jwt.v1";
export const AUTH_EXPIRES_KEY = "nerya.admin_jwt_expires_at.v1";
export const AUTH_EVENT = "nerya:auth_changed";

function browser(): boolean {
  return typeof window !== "undefined";
}

function normaliseHost(host: string): string {
  const raw = (host || "").trim().toLowerCase();
  if (!raw) return "";
  if (raw.startsWith("[") && raw.includes("]")) return raw.slice(1, raw.indexOf("]"));
  if (raw === "::1") return raw;
  if (raw.indexOf(":") === raw.lastIndexOf(":")) return raw.split(":")[0];
  return raw.split(":")[0];
}

export function isLocalDashboardHost(hostname?: string): boolean {
  const host = normaliseHost(hostname ?? (browser() ? window.location.hostname : ""));
  return !host || host === "localhost" || host === "::1" || host === "0.0.0.0" || host.startsWith("127.");
}

export function getStoredAuthToken(): string {
  if (!browser()) return "";
  try {
    return window.localStorage.getItem(AUTH_TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function setStoredAuthToken(token: string, expiresAt?: number): void {
  if (!browser()) return;
  try {
    window.localStorage.setItem(AUTH_TOKEN_KEY, token);
    if (expiresAt) window.localStorage.setItem(AUTH_EXPIRES_KEY, String(expiresAt));
    window.dispatchEvent(new Event(AUTH_EVENT));
  } catch {
    // ignore storage failures; requests will simply remain unauthenticated.
  }
}

export function clearStoredAuthToken(): void {
  if (!browser()) return;
  try {
    window.localStorage.removeItem(AUTH_TOKEN_KEY);
    window.localStorage.removeItem(AUTH_EXPIRES_KEY);
    window.dispatchEvent(new Event(AUTH_EVENT));
  } catch {
    // ignore
  }
}

export function authHeaders(base?: HeadersInit): Headers {
  const headers = new Headers(base);
  const token = getStoredAuthToken();
  if (token && !headers.has("authorization") && !headers.has("x-nerya-token")) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  return headers;
}

export function redirectToLogin(): void {
  if (!browser() || isLocalDashboardHost()) return;
  const path = `${window.location.pathname}${window.location.search || ""}`;
  if (window.location.pathname === "/login") return;
  window.location.assign(`/login?next=${encodeURIComponent(path)}`);
}

export function handleAuthFailure(status: number): void {
  if (status !== 401 && status !== 403) return;
  if (isLocalDashboardHost()) return;
  clearStoredAuthToken();
  redirectToLogin();
}
