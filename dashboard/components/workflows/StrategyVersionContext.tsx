"use client";

import { copy as i18nCopy } from "../../lib/i18n";

import { useEffect, useState } from "react";
import { useLocale } from "next-intl";
import Link from "next/link";
import { callApi } from "../../lib/clientApi";
import type { WorkflowView } from "../../lib/workflowTypes";

export function StrategyVersionContext({ workflow, dirty }: { workflow: WorkflowView; dirty: boolean }) {
  const zh = useLocale().startsWith("zh");
  const [open,setOpen] = useState(false), [refresh,setRefresh] = useState(0);
  const [published,setPublished] = useState(""), [running,setRunning] = useState(""), [state,setState] = useState("");
  const [loading,setLoading] = useState(false);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    let disposed = false;
    const timer = setTimeout(() => controller.abort(),10000);
    setLoading(true); setPublished(""); setRunning(""); setState("");
    const id=encodeURIComponent(workflow.strategy_id);
    void Promise.allSettled([
      callApi<{ ok:boolean; package_hash?:string }>(`/strategies/runtime/status?strategy_id=${id}`,{ signal:controller.signal }),
      callApi<{ ok:boolean; package_hash?:string; state?:string }>(`/strategies/runtime/service/status?strategy_id=${id}`,{ signal:controller.signal }),
    ]).then(([pkg,service]) => {
      if (disposed) return;
      if (pkg.status === "fulfilled" && pkg.value.ok) setPublished(pkg.value.package_hash || "");
      if (service.status === "fulfilled" && service.value.ok) { setState(service.value.state || "");
        if (["running","starting","restarting","stopping"].includes(service.value.state || "")) setRunning(service.value.package_hash || ""); }
    }).finally(() => { clearTimeout(timer); if (!disposed) setLoading(false); });
    return () => { disposed=true; clearTimeout(timer); controller.abort(); };
  },[open,workflow.strategy_id,workflow.revision,refresh]);
  const candidate=workflow.source.proposal_id;
  const unknown=i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.001");
  return <div className="border-b border-[color:var(--line)] px-5 py-2 text-xs leading-6 text-[color:var(--text-muted)]" data-testid="strategy-version-context">
    <div className="flex flex-wrap items-center gap-x-3"><strong className="text-[color:var(--text-base)]">{candidate ? (i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.002")) : (i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.003"))}</strong>
      <span>{dirty ? (i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.004")) : candidate ? (i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.005")) : (i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.006"))}</span></div>
    <details open={open} onToggle={event => setOpen(event.currentTarget.open)}><summary className="w-fit cursor-pointer rounded focus-visible:outline focus-visible:outline-2 focus-visible:outline-[color:var(--violet)]">{i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.007")}</summary>
      {open && <div className="space-y-2 py-2">
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4">
          <dt>{i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.008")}</dt><dd className="break-all font-mono">{workflow.revision}</dd>
          {candidate && <><dt>{i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.009")}</dt><dd className="break-all font-mono">{candidate}</dd></>}
          <dt>{i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.010")}</dt><dd className="break-all font-mono">{loading ? "…" : published || unknown}</dd>
          <dt>{i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.011")}</dt><dd className="break-all font-mono">{loading ? "…" : running || (["stopped","finished"].includes(state) ? (i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.012")) : unknown)}</dd>
        </dl>
        {running && published && running !== published && <p role="status">{i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.013")}</p>}
        <p>{i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.014")}</p>
        <p>{i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.015")}</p>
        <div className="flex flex-wrap gap-3"><button type="button" disabled={loading} onClick={() => setRefresh(value=>value+1)}>{i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.016")}</button>
          {candidate && <Link href={`/self-evolution?tab=proposals&proposal_id=${encodeURIComponent(candidate)}`}>{i18nCopy(zh, "copy.components_workflows_StrategyVersionContext.017")}</Link>}</div>
      </div>}
    </details>
  </div>;
}
