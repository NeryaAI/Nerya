"use client";

import type { ChatAttachment } from "./chat";

const LEGACY_KEY = "nerya.compose.draft.v1";
const KEY = "nerya.compose.draft.v2";
export type ComposeDraft = { text: string; attachments: ChatAttachment[]; autoSend: boolean };
let fallback: ComposeDraft | null = null;

/** Attachments are already uploaded: persist references, never megabytes of base64. */
export function setComposeDraftPayload(draft: ComposeDraft): void {
  if (typeof window === "undefined") return;
  const attachments = draft.attachments.map(({ data_url: _data, text: _text, ...metadata }) => metadata);
  fallback = { ...draft, attachments };
  try {
    window.sessionStorage.removeItem(LEGACY_KEY);
    window.sessionStorage.setItem(KEY, JSON.stringify(fallback));
  } catch { /* The in-memory handoff still works when storage is blocked. */ }
}

export function takeComposeDraftPayload(): ComposeDraft | null {
  if (typeof window === "undefined") return null;
  let draft = fallback;
  fallback = null;
  try {
    const raw = window.sessionStorage.getItem(KEY);
    const legacy = window.sessionStorage.getItem(LEGACY_KEY);
    window.sessionStorage.removeItem(KEY);
    window.sessionStorage.removeItem(LEGACY_KEY);
    if (!draft && raw) {
      const value: unknown = JSON.parse(raw);
      if (value && typeof value === "object" && "text" in value && typeof value.text === "string") {
        const saved = value as Partial<ComposeDraft>;
        draft = {
          text: value.text,
          autoSend: saved.autoSend === true,
          attachments: Array.isArray(saved.attachments) ? saved.attachments.filter((item) =>
            item && typeof item.id === "string" && typeof item.name === "string" && typeof item.artifact_uri === "string",
          ) : [],
        };
      }
    } else if (!draft && legacy) draft = { text: legacy, attachments: [], autoSend: true };
  } catch { /* Return the memory copy, if available. */ }
  return draft;
}

/** Compatibility for command palette and older text-only callers. */
export function setComposeDraft(text: string): void {
  setComposeDraftPayload({ text, attachments: [], autoSend: true });
}
export function takeComposeDraft(): string {
  return takeComposeDraftPayload()?.text ?? "";
}
