import type { LlmProviderProfile } from "../../lib/clientApi";

export type ProviderDraft = { baseUrl: string; kind: string; key: string };
type ProviderProfile = LlmProviderProfile & { declared?: Partial<LlmProviderProfile> };

export function providerDraftOf(profile: ProviderProfile): ProviderDraft {
  const source = profile.declared || profile;
  return { baseUrl: source.base_url || "", kind: source.kind || "", key: "" };
}

export function providerPatch(profile: ProviderProfile, draft: ProviderDraft): LlmProviderProfile {
  const patch: LlmProviderProfile = { provider: profile.provider };
  const original = providerDraftOf(profile);
  if (draft.baseUrl.trim() !== original.baseUrl) patch.base_url = draft.baseUrl.trim();
  if (draft.kind !== original.kind) patch.kind = draft.kind;
  const key = draft.key.trim();
  if (key) {
    if (key.startsWith("vault://")) patch.provider_key_ref = key;
    else patch.provider_key = key;
  }
  return patch;
}
