"use client";

/**
 * Cross-route compose draft.
 *
 * The Codex-style command home owns the primary composer. When another
 * surface (the ⌘K palette, a sidebar action, a suggestion link) wants to
 * pre-fill that composer it stashes the text here and routes to the home,
 * which drains the draft on mount. sessionStorage keeps it tab-scoped and
 * survives the client-side navigation without leaking into the next visit.
 */

const KEY = "nerya.compose.draft.v1";

export function setComposeDraft(text: string): void {
  if (typeof window === "undefined") return;
  try {
    if (text) window.sessionStorage.setItem(KEY, text);
    else window.sessionStorage.removeItem(KEY);
  } catch {
    /* ignore private-mode / quota errors */
  }
}

export function takeComposeDraft(): string {
  if (typeof window === "undefined") return "";
  try {
    const value = window.sessionStorage.getItem(KEY) || "";
    if (value) window.sessionStorage.removeItem(KEY);
    return value;
  } catch {
    return "";
  }
}
