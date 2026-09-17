"use client";

import { useEffect, useState, type ReactNode } from "react";
import * as Popover from "@radix-ui/react-popover";
import { WorkflowIcon, useWorkflowText } from "./WorkflowCanvas";
import type { WorkflowKind, WorkflowNode } from "../../lib/workflowTypes";
import { cardTitle } from "../../lib/workflowPresentation";
import ui from "./WorkflowNative.module.css";

export function WorkflowHelp({ label, children }: { label: string; children: ReactNode }) {
  return <Popover.Root><Popover.Trigger asChild><button type="button" className={ui.helpButton} aria-label={label} title={label}>?</button></Popover.Trigger><Popover.Portal><Popover.Content className={ui.helpPopover} sideOffset={8} collisionPadding={16} aria-label={label}><h4>{label}</h4>{children}<Popover.Arrow className={ui.popoverArrow} /></Popover.Content></Popover.Portal></Popover.Root>;
}

/** A draft stays bound to the resource chosen when typing began. Selecting
 * another card must not silently send that draft to a different resource. */
export function WorkflowCommand({ node, disabled, dirty, onSubmit, onDraftChange }: {
  node?: WorkflowNode; disabled: boolean; dirty: boolean;
  onSubmit: (text: string, nodeId: string | null) => void; onDraftChange: (dirty: boolean) => void;
}) {
  const t = useWorkflowText();
  const [text, setText] = useState("");
  const [scope, setScope] = useState<{ id: string; title: string; kind: WorkflowKind } | null>(null);
  const [locked, setLocked] = useState(false);
  useEffect(() => { onDraftChange(!!text.trim()); }, [text, onDraftChange]);
  const target = locked ? scope : node ? { id: node.id, title: cardTitle(node, t), kind: node.kind } : null;
  function change(value: string) {
    if (!locked) { setScope(target); setLocked(true); }
    setText(value); if (!value) setLocked(false);
  }
  function submit() { if (!text.trim() || disabled || dirty) return; onSubmit(text.trim(), target?.id || null); }
  return <div className={ui.commandDock} data-testid="workflow-command"><form className={ui.command} onSubmit={(event) => { event.preventDefault(); submit(); }}>
    <textarea aria-label={t("描述策略修改", "Describe a strategy change")} placeholder={target ? t(`想怎么调整「${target.title}」？`, `How should ${target.title} change?`) : t("描述你想调整的策略…", "Describe a change to this strategy…")} value={text} maxLength={3000} rows={1} disabled={disabled} onChange={(event) => change(event.target.value)} onKeyDown={(event) => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter" && !event.nativeEvent.isComposing) { event.preventDefault(); submit(); } }} />
    <div className={ui.commandBar}><span className={ui.scope}><WorkflowIcon kind={target?.kind || "strategy"} size={14} />{target?.title || t("当前策略", "This strategy")}{target && <button type="button" aria-label={t("改为整个策略", "Target the whole strategy")} onClick={() => { setScope(null); setLocked(true); }}>×</button>}</span><span className={ui.commandHint}>{dirty ? t("先保存手动修改", "Save manual edits first") : t("在 Agent 对话中继续", "Continue in Agent chat")}</span>{<button type="submit" className={ui.sendButton} disabled={disabled || dirty || !text.trim()} aria-label={t("交给 Agent", "Continue with Agent")} title={t("交给 Agent · ⌘/Ctrl Enter", "Continue with Agent · ⌘/Ctrl Enter")}><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true"><path d="M12 20V4m-6 6 6-6 6 6" /></svg></button>}</div>
  </form></div>;
}
