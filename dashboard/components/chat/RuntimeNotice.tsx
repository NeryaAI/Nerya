"use client";
import { copy as i18nCopy } from "../../lib/i18n";

import { useEffect,useState } from "react";
import { isDesktop,desktopStatus } from "../../lib/desktop";
import { useLocale } from "next-intl";
import type { useWorkbench } from "./useWorkbench";
export function RuntimeNotice({workbench,diagnostics=false}:{workbench:ReturnType<typeof useWorkbench>;diagnostics?:boolean}) {
  const zh=useLocale().startsWith("zh");
  const {connection,runtime}=workbench;
  const [desktop,setDesktop]=useState("");
  useEffect(()=>{if(isDesktop())void desktopStatus().then(s=>setDesktop(s.desktop_version||"unknown")).catch(()=>setDesktop("unavailable"));},[]);
  if(diagnostics) return <div className="space-y-2 py-1 text-xs text-[color:var(--text-muted)]" data-testid="runtime-diagnostics"><p>API <code>{runtime?.build_id || "—"}</code> · {i18nCopy(zh, "copy.components_chat_RuntimeNotice.001")} {runtime?.protocol_version || "—"}</p><p>Dashboard <code>{process.env.NEXT_PUBLIC_NERYA_BUILD_ID || "development"}</code></p>{desktop&&<p>Desktop {desktop}</p>}</div>;
  if(connection==="online"||connection==="connecting") return null;
  return <div role="status" className="flex flex-wrap items-center justify-between gap-2 border-b border-[color:var(--line)] px-4 py-2 text-xs text-[color:var(--text-muted)]" data-testid="runtime-notice">
    <span>{connection==="incompatible" ? (i18nCopy(zh, "copy.components_chat_RuntimeNotice.002")) : (i18nCopy(zh, "copy.components_chat_RuntimeNotice.003"))}</span>
    <button type="button" className="min-h-11 underline" onClick={workbench.refresh}>{i18nCopy(zh, "copy.components_chat_RuntimeNotice.004")}</button>
    {runtime && <details><summary>{i18nCopy(zh, "copy.components_chat_RuntimeNotice.005")}</summary><p>{runtime.build_id} · {runtime.protocol_version}</p></details>}
  </div>;
}
