import type { LlmProviderProfile, LlmRouteConfig, LlmTierConfig } from "../../lib/clientApi";

export type ModelRouteState = LlmRouteConfig & {
  provider_key_env?: string;
  declared?: Partial<LlmRouteConfig> & { provider_key_env?: string };
  effective?: LlmRouteConfig;
  source?: Record<string, string>;
};
export type ModelProfileState = LlmProviderProfile & {
  declared?: Partial<LlmProviderProfile> & { kind?: string };
  kind?: string;
};

export function moveModelRoute<T>(routes: T[], from: number, to: number): T[] {
  if (!Number.isInteger(from) || !Number.isInteger(to) || from === to ||
      from < 0 || to < 0 || from >= routes.length || to >= routes.length) return routes;
  const reordered = [...routes];
  reordered.splice(to, 0, ...reordered.splice(from, 1));
  return reordered;
}

// Keep the resolved connection as evidence, never as an editable override.
export function editableRoute(route: ModelRouteState): ModelRouteState {
  if (!route.declared) return route; // Older servers keep their existing contract.
  return {
    ...route,
    base_url: route.declared.base_url || "",
    provider_key_ref: route.declared.provider_key_ref || "",
    provider_key_env: route.declared.provider_key_env || "",
    provider_key_refs: (route.declared.provider_key_ref || "").split(",").map(v => v.trim()).filter(Boolean),
    kind: route.declared.kind || "",
    provider_native_web_search: route.declared.provider_native_web_search,
  };
}

export function explicitRoute(route: ModelRouteState): LlmRouteConfig {
  const { declared, effective, source: _source, ...values } = route;
  if (!declared || !effective) return values;
  for (const key of ["context_window", "reasoning_effort"] as const) {
    if (!(key in declared) && values[key] === effective[key]) delete values[key];
  }
  return values;
}

export function tierPolicy(row: LlmTierConfig & { declared?: Partial<LlmTierConfig> }) {
  if (!row.declared) return { reasoning_effort: "", context_window: row.context_window };
  return Object.fromEntries(["context_window", "reasoning_effort", "provider_native_web_search"]
    .filter(key => key in row.declared!).map(key => [key, row.declared![key as keyof LlmTierConfig]]));
}

export function connectionPatch(profile: ModelProfileState | undefined, provider: string, url: string, key: string, kind = ""): ModelProfileState {
  const patch: ModelProfileState = { provider };
  if (url.trim() !== (profile?.base_url || "")) patch.base_url = url.trim();
  if (key.trim() && key.trim() !== profile?.provider_key_ref) {
    if (key.trim().startsWith("vault://")) patch.provider_key_ref = key.trim();
    else patch.provider_key = key.trim();
  }
  if (kind && kind !== profile?.kind) patch.kind = kind;
  return patch;
}
