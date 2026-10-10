"use client";
import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { callApi } from "../lib/clientApi";
import type { AssistantDraft } from "../lib/assistants";

type Tool = {name: string; description: string; namespace: string; read_only: boolean};
export function AssistantToolAccess({policyText, onChange}: {policyText: string; onChange: (value: string) => void}) {
  const t = useTranslations("assistants");
  const [tools, setTools] = useState<Tool[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setError(false);
    callApi<{ok: boolean; tools: Tool[]}>("/assistants/tools").then(result => { if (active) { setTools(result.tools || []); setError(!result.ok); } }).catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, [attempt]);
  let policy: AssistantDraft["tool_policy"];
  try { policy = JSON.parse(policyText); if (!Array.isArray(policy.deny)) return null; } catch { return null; }
  return <div className="mb-5 space-y-3">
    <input type="search" className="input-dark w-full" aria-label={t("findTool")} placeholder={t("findTool")} value={query} onChange={e => setQuery(e.target.value)} />
    {error && <button type="button" className="btn btn-ghost" onClick={() => setAttempt(value => value + 1)}>{t("retryTools")}</button>}
    <div className="max-h-64 overflow-y-auto">
      {tools.filter(tool => (tool.name + " " + tool.description).toLowerCase().includes(query.toLowerCase())).map(tool => <label key={tool.name} className="flex items-start justify-between gap-4 border-b border-[color:var(--line)] py-3 text-sm">
        <span className="min-w-0"><span className="block break-all font-medium">{tool.name}</span><span className="line-clamp-2 text-xs text-[color:var(--text-muted)]">{tool.description}</span></span>
        <input type="checkbox" className="mt-1" checked={!policy.deny.includes(tool.name)} onChange={e => {
          const deny = policy.deny.filter(name => name !== tool.name);
          if (!e.target.checked) deny.push(tool.name);
          onChange(JSON.stringify({...policy, deny}, null, 2));
        }} />
      </label>)}
    </div>
    <p className="text-xs text-[color:var(--text-muted)]">{t("toolSwitchHint")}</p>
  </div>;
}
