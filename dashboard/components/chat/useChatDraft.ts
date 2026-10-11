"use client";

import { useCallback, useEffect, useRef, useState, type SetStateAction } from "react";
import type { ChatAttachment, ChatRunSettings } from "../../lib/chat";
import { draftWindowId, savedDrafts, saveDurableDraft, type SavedDraft } from "../../lib/durableDrafts";
import { captureChatDraft, clearChatDraftIfUnchanged, EMPTY_CHAT_DRAFT, hasChatDraft, readChatDraft, writeChatDraft, type ChatDraft, type DraftReceipt } from "../../lib/chatDraft";
import { getWorkspaceIdentity, useWorkspaceIdentity, workspaceGeneration } from "../../lib/workspaceIdentity";

/** Text, attachments and settings have one workspace/scope/revision owner. */
export function useChatDraft(scope: string) {
  const workspaceId = useWorkspaceIdentity();
  const generation = workspaceGeneration();
  const [snapshot, setSnapshot] = useState<{ workspace: string | null; generation: number; scope: string; draft: ChatDraft }>({ workspace: null, generation, scope, draft: EMPTY_CHAT_DRAFT });
  const latest = useRef(snapshot);
  latest.current = snapshot;
  const active = useRef({ scope, workspaceId });
  active.current = { scope, workspaceId };
  const [recovery, setRecovery] = useState<SavedDraft[]>([]);
  const [storageError, setStorageError] = useState(false);
  useEffect(() => {
    const error = () => setStorageError(true);
    window.addEventListener("nerya:draft-storage-unavailable", error);
    return () => { window.removeEventListener("nerya:draft-storage-unavailable", error); };
  }, []);
  useEffect(() => {
    let disposed = false;
    setRecovery([]); setStorageError(false);
    let draft = readChatDraft(scope, workspaceId);
    // Preserve text typed on the initial connection, but never move an old workspace's draft.
    const local = latest.current;
    if (workspaceId && generation === 1 && local.workspace === null && local.scope === scope && hasChatDraft(local.draft)) {
      draft = writeChatDraft(scope, local.draft, workspaceId);
      saveDurableDraft(workspaceId, scope, draft);
    }
    setSnapshot({ workspace: workspaceId, generation, scope, draft });
    const revision = draft.revision || 0;
    if (workspaceId) savedDrafts(workspaceId, scope).then(rows => {
      if (disposed || workspaceGeneration() !== generation) return;
      const own = rows.find(row => row.windowId === draftWindowId());
      const current = readChatDraft(scope, workspaceId);
      if (own && revision === 0 && !hasChatDraft(current) && (current.revision || 0) === revision) {
        const restored = writeChatDraft(scope, own.draft, workspaceId);
        setSnapshot({ workspace: workspaceId, generation, scope, draft: restored });
      }
      setRecovery(rows.filter(row => row.windowId !== draftWindowId() && hasChatDraft(row.draft)));
    }).catch(() => { if (!disposed) setStorageError(true); });
    return () => { disposed = true; };
  }, [scope, workspaceId, generation]);
  const update = useCallback((apply: (draft: ChatDraft) => ChatDraft) => {
    if (workspaceId !== getWorkspaceIdentity() || generation !== workspaceGeneration()) return;
    const current = workspaceId ? readChatDraft(scope, workspaceId)
      : latest.current.scope === scope && latest.current.workspace === null ? latest.current.draft : EMPTY_CHAT_DRAFT;
    const draft = writeChatDraft(scope, apply(current), workspaceId);
    if (workspaceId) saveDurableDraft(workspaceId, scope, draft);
    if (active.current.scope === scope && active.current.workspaceId === workspaceId) {
      latest.current = { workspace: workspaceId, generation, scope, draft };
      setSnapshot(latest.current);
    }
  }, [scope, workspaceId, generation]);
  const setText = useCallback((value: SetStateAction<string>) => update(draft => ({ ...draft,
    text: typeof value === "function" ? value(draft.text) : value })), [update]);
  const setAttachments = useCallback((value: SetStateAction<ChatAttachment[]>) => update(draft => ({ ...draft,
    attachments: typeof value === "function" ? value(draft.attachments) : value })), [update]);
  const setSettings = useCallback((settings: ChatRunSettings) => update(draft => ({ ...draft, settings })), [update]);
  const draft = snapshot.scope === scope && snapshot.workspace === workspaceId && snapshot.generation === generation ? snapshot.draft : EMPTY_CHAT_DRAFT;
  function clearIfUnchanged(receipt: DraftReceipt | null) {
    if (!clearChatDraftIfUnchanged(receipt) || !receipt) return false;
    const next = readChatDraft(receipt.scope, receipt.workspace);
    saveDurableDraft(receipt.workspace, receipt.scope, next);
    if (active.current.scope === receipt.scope && active.current.workspaceId === receipt.workspace) setSnapshot({ workspace: receipt.workspace, generation: receipt.generation, scope: receipt.scope, draft: next });
    return true;
  }
  return { text: draft.text, attachments: draft.attachments, settings: draft.settings, revision: draft.revision || 0,
    workspaceId, ready: Boolean(workspaceId && snapshot.workspace === workspaceId && snapshot.scope === scope && snapshot.generation === generation),
    setSettings, setText, setAttachments, replace: (value: ChatDraft) => update(() => value),
    capture: () => captureChatDraft(scope), clearIfUnchanged,
    recovery: snapshot.workspace === workspaceId && snapshot.scope === scope && snapshot.generation === generation ? recovery : [], storageError,
    restore: (row: SavedDraft) => { if (row.workspace === workspaceId && row.scope === scope) { update(() => row.draft); setRecovery([]); } },
    dismissRecovery: () => setRecovery([]) };
}
