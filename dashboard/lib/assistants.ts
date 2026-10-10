import { callApi, type AgentSession } from "./clientApi";

export const ASSISTANTS_CHANGED = "nerya:assistants-changed";
export const assistantPermissions = ["research_allowed", "memory_allowed", "browser_allowed", "files_allowed", "shell_allowed"] as const;
export type AssistantProfile = {
  id: string; name: string; role: string; instructions: string; avatar: string; enabled: boolean;
  model_provider: string; model_id: string; model_tier: string;
  tool_policy: { allow_groups: string[][]; deny: string[] };
  revision: number; created_at: number; updated_at: number;
} & Record<typeof assistantPermissions[number], boolean>;
export type AssistantDraft = Omit<AssistantProfile, "id" | "revision" | "created_at" | "updated_at">;
export type AssistantInventory = { ok: boolean; assistants: AssistantProfile[]; workspace: string;
  channels: { channel: string; kind: string; assistant_id: string; enabled: boolean }[]; error?: string };

export function assistantDraft(profile?: AssistantProfile): AssistantDraft {
  if (profile) {
    const { id, revision, created_at, updated_at, ...draft } = profile;
    return structuredClone(draft);
  }
  return { name: "", role: "", instructions: "", avatar: "", enabled: true, model_provider: "", model_id: "", model_tier: "",
    research_allowed: true, memory_allowed: true, browser_allowed: true, files_allowed: true, shell_allowed: false,
    tool_policy: { allow_groups: [], deny: [] } };
}

async function checked<T extends { ok: boolean; error?: string }>(result: Promise<T>): Promise<T> {
  const data = await result;
  if (!data.ok) throw new Error(data.error || "assistant_request_failed");
  return data;
}
export const assistantsApi = {
  home: (assistant_id: string) => checked(callApi<{ok: boolean; session_id: string; assistant_id: string}>("/assistants/home", {method: "POST", body: {assistant_id}})),
  list: () => checked(callApi<AssistantInventory>("/assistants")),
  save: (profile: Partial<AssistantDraft>, current?: AssistantProfile) => checked(callApi<{ok: boolean; assistant: AssistantProfile; error?: string}>("/assistants/save", {
    method: "POST", body: { profile, ...(current ? { id: current.id, expected_revision: current.revision } : {}) },
  })),
  duplicate: (id: string, name: string) => checked(callApi<{ok: boolean; assistant: AssistantProfile; error?: string}>("/assistants/duplicate", {method: "POST", body: {id, name}})),
  archive: (current: AssistantProfile) => checked(callApi<{ok: boolean; error?: string}>("/assistants/archive", {method: "POST", body: {id: current.id, expected_revision: current.revision}})),
  conversation: (assistant_id: string) => checked(callApi<{ok: boolean; session_id: string; error?: string}>("/assistants/conversations", {method: "POST", body: {assistant_id}})),
  conversations: (assistant_id: string) => checked(callApi<{ok: boolean; sessions: (AgentSession & {title?: string})[]; error?: string}>("/assistants/conversations?assistant_id=" + encodeURIComponent(assistant_id))),
  session: (session_id: string) => checked(callApi<{ok: boolean; assistant: AssistantProfile | null; conversation?: {conversation_kind?: string; parent_session_id?: string}; error?: string}>("/assistants/session?session_id=" + encodeURIComponent(session_id))),
};
