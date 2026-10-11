"use client";
import { Icon as NeryaGlyph } from "../icons";

import { ChoiceSelect } from "../ChoiceSelect";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { callApi } from "../../lib/clientApi";
import { asObject } from "../../lib/workflowPresentation";
import { fieldLabel } from "../../lib/agentConversation";
import { activities, activityState, durationText, publicRuntimeEvents, runtimeTools, parallelMembers, type Activity, type Facts } from "../../lib/workflowActivity";
import { useWorkflowText } from "./WorkflowCanvas";
import { useLocale, useTranslations } from "next-intl";
import { WorkflowRunCanvas } from "./WorkflowRunCanvas";
import { rememberInvocation, selectedInvocation, recordedTurnReplay, type RecordedCall } from "../../lib/workflowReplay";
import replayStyles from "./WorkflowReplay.module.css";
import { WorkflowHelp } from "./WorkflowNative";
import styles from "./WorkflowActivity.module.css";
import { TaskRunHistory } from "../chat/TaskRunHistory";
import {TaskFinancialGrants} from '../TaskFinancialGrants';
import { RunWorkspace } from "./RunWorkspace";

function Evidence({ value }: { value: unknown }) {
  const t = useWorkflowText();
  const zh = useLocale().startsWith("zh");
  if (value === undefined || value === null) return <span className={styles.muted}>{t("copy.components_workflows_WorkflowActivity.001")}</span>;
  if (typeof value !== "object") return <p className={styles.text}>{typeof value === "boolean" ? t(value ? "copy.workflowActivity.boolean.true" : "copy.workflowActivity.boolean.false") : String(value)}</p>;
  return <dl className={styles.facts}>{Object.entries(value).slice(0, 30).map(([key, item]) => <div className={styles.fact} key={key}><dt>{fieldLabel(key, zh)}</dt><dd>{item !== null && typeof item === "object" ? <details><summary>{Array.isArray(item) ? t("copy.components_workflows_WorkflowActivity.002", { value0: item.length }) : t("copy.components_workflows_WorkflowActivity.003")}</summary><pre className={styles.raw}>{JSON.stringify(item, null, 2)}</pre></details> : <Evidence value={item} />}</dd></div>)}{Object.keys(value).length > 30 && <p className={styles.muted}>{t("copy.components_workflows_WorkflowActivity.004")}</p>}</dl>;
}
function RunDetails({ strategyId, item, live, onEdit, roleNames }: { strategyId: string; item: Activity; live: boolean; onEdit: (kind: "agent" | "script") => void; roleNames: Record<string, string> }) {
  const t = useWorkflowText();
  const [detail, setDetail] = useState<Facts>({});
  const [events, setEvents] = useState<Facts[]>([]);
  const [error, setError] = useState("");
  const running = ["running", "queued"].includes(item.status);
  const turnId = String(item.row.turn_id || "");
  const sessionId = String(item.row.session_id || "");
  useEffect(() => {
    if (item.kind !== "agent") return;
    let disposed = false; let timer: ReturnType<typeof setTimeout>; let active: AbortController | undefined; let cursor = 0;
    setDetail({}); setEvents([]); setError("");
    async function poll() {
      if (disposed) return;
      if (document.hidden) { timer = setTimeout(poll, 5000); return; }
      active = new AbortController(); const timeout = setTimeout(() => active?.abort(), 15000);
      try {
        const out = await callApi<Facts>(`/strategies/runtime/agent_task?strategy_id=${encodeURIComponent(strategyId)}&task_id=${encodeURIComponent(item.id)}&include_prompt=1`, { signal: active.signal });
        if (out.ok === false) throw new Error(String(out.error || "Task unavailable"));
        if (out.strategy_id !== strategyId || out.task_id !== item.id) throw new Error("Recorded task identity mismatch");
        if (!disposed) setDetail(out);
        if (running && turnId && sessionId) {
          const stream = await callApi<Facts>(`/agent/stream/events?session_id=${encodeURIComponent(sessionId)}&after_seq=${cursor}&limit=200`, { signal: active.signal });
          cursor = Math.max(cursor, Number(stream.cursor ?? stream.latest_seq) || 0);
          const incoming = publicRuntimeEvents(stream.events, turnId);
          if (!disposed) setEvents((previous) => [...new Map([...previous, ...incoming].map((event) => [String(event.event_id || event.seq), event])).values()].slice(-500));
        }
        if (!disposed) setError("");
      } catch (reason) { if (!disposed) setError(String(reason)); }
      finally { clearTimeout(timeout); if (!disposed && live && running) timer = setTimeout(poll, 3000); }
    }
    void poll();
    return () => { disposed = true; clearTimeout(timer); active?.abort(); };
  }, [strategyId, item.id, item.kind, item.status, turnId, sessionId, live, running]);
  const detailTask = asObject(detail.task);
  const detailNewer = (Date.parse(String(detailTask.ts || detailTask.finished_at || "")) || 0) >= (Date.parse(String(item.row.ts || item.row.finished_at || "")) || 0);
  const task = detailNewer ? { ...item.row, ...detailTask } : { ...detailTask, ...item.row };
  const merged = { ...item, status: String(task.status || item.status), row: task };
  const state = activityState(merged, t);
  const isRunning = ["running", "queued"].includes(merged.status);
  const recorded = recordedTurnReplay(detail.recorded_turn);
  const tools = runtimeTools(merged, events);
  const calls = [...new Map<string, RecordedCall>([
    ...tools.map((tool) => [tool.id, { ...tool, status: tool.state }] as const),
    ...recorded.calls.map((call) => [call.id, call] as const),
  ]).values()];
  const members = parallelMembers(merged, events, detail.team_snapshot);
  const memberState = (value: string) => ({ queued: t("copy.components_workflows_WorkflowActivity.005"), running: t("copy.components_workflows_WorkflowActivity.006"), returned: t("copy.components_workflows_WorkflowActivity.007"), error: t("copy.components_workflows_WorkflowActivity.008"), unknown: t("copy.components_workflows_WorkflowActivity.009") }[value] || value);
  const reply = typeof task.final_text === "string" && task.final_text.trim() ? task.final_text : String(asObject(task.decision).text || "");
  const partial = events.filter((event) => event.kind === "message.delta").map((event) => String(event.text || "")).join("").slice(-30000);
  const needsApproval = String(task.stopped_reason || "").includes("approval") || (isRunning && events.some((event) => event.kind === "approval.request"));
  const stamp = item.ts && !Number.isNaN(Date.parse(item.ts)) ? new Date(item.ts).toLocaleString() : t("copy.components_workflows_WorkflowActivity.010");
  const errorMessage = String(asObject(task.error).message || (typeof task.error === "string" ? task.error : "") || (state.tone === "error" ? item.reason : ""));
  const pending = [...tools].reverse().find((tool) => tool.state === "running");
  const chosenPath = asObject(task.metadata).path;
  const selectedRoles = asObject(task.metadata).selected_roles;
  const hasPath = typeof chosenPath === "string" && chosenPath.trim().length > 0;
  const toolTitles: Record<string, string> = { market_data: t("copy.components_workflows_WorkflowActivity.011"), team_run: t("copy.components_workflows_WorkflowActivity.012"), risk_check: t("copy.components_workflows_WorkflowActivity.013"), portfolio_summary: t("copy.components_workflows_WorkflowActivity.014"), strategy_history: t("copy.components_workflows_WorkflowActivity.015") };
  return <div data-testid="workflow-run-detail"><RunWorkspace
    title={item.kind === "agent" ? t("copy.components_workflows_WorkflowActivity.016") : t("copy.components_workflows_WorkflowActivity.017")}
    subtitle={<>{stamp}{typeof task.duration_ms === "number" ? ` · ${durationText(task.duration_ms,t)}` : ""}</>}
    status={<span className={styles.badge} data-tone={state.tone}>{state.label}</span>}
    result={<>

    {hasPath && <section className={styles.response} data-testid="workflow-selected-path"><div className={styles.responseTitle}>{t("copy.components_workflows_WorkflowActivity.019")}</div><p>{String(task.reason || chosenPath)}</p>{Array.isArray(selectedRoles) && <p className={styles.muted}>{t("copy.components_workflows_WorkflowActivity.020")}{selectedRoles.length ? selectedRoles.map((name) => roleNames[String(name)] || String(name)).join("、") : t("copy.components_workflows_WorkflowActivity.021")}</p>}</section>}
    {needsApproval && <p className={styles.notice}>{t("copy.components_workflows_WorkflowActivity.022")}{sessionId && <Link href={`/chat/${encodeURIComponent(sessionId)}`}>{t("copy.components_workflows_WorkflowActivity.023")} <NeryaGlyph name="arrowRight" size={16} /></Link>}</p>}
    {isRunning && <div className={styles.now} role="status"><span className={styles.pulse} /><span>{pending ? `${toolTitles[pending.name] || pending.name}…` : partial ? t("copy.components_workflows_WorkflowActivity.024") : t("copy.components_workflows_WorkflowActivity.025")}</span><WorkflowHelp label={t("copy.components_workflows_WorkflowActivity.026")}><p>{t("copy.components_workflows_WorkflowActivity.027")}</p></WorkflowHelp></div>}
    {error && <details className={styles.error} open><summary>{t("copy.components_workflows_WorkflowActivity.028")}</summary><p>{error}</p></details>}
    {errorMessage && <section className={styles.error}><strong>{t("copy.components_workflows_WorkflowActivity.029")}</strong><p>{errorMessage}</p></section>}
    {state.tone === "warning" && task.stopped_reason != null && <p className={styles.notice}>{t("copy.components_workflows_WorkflowActivity.030")}{String(task.stopped_reason)}</p>}
    {item.kind === "agent" && (reply || partial) ? <section className={styles.response} data-testid="workflow-run-response"><div className={styles.responseTitle}>{reply ? t("copy.components_workflows_WorkflowActivity.033") : t("copy.components_workflows_WorkflowActivity.034")}</div><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ children, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer">{children}</a> }}>{reply || partial}</ReactMarkdown></section> : item.kind === "script" && task.outputs != null ? <section className={styles.response}><div className={styles.responseTitle}>{t("copy.components_workflows_WorkflowActivity.035")}</div><Evidence value={task.outputs} /></section> : !isRunning && !errorMessage && !hasPath && <p className={styles.text}>{item.reason || t("copy.components_workflows_WorkflowActivity.036")}</p>}
    <div className={styles.links}>{sessionId && <Link href={`/chat/${encodeURIComponent(sessionId)}`}>{t("copy.components_workflows_WorkflowActivity.050")} <NeryaGlyph name="arrowUpRight" size={16} /></Link>}<button type="button" onClick={() => onEdit(item.kind)}>{t("copy.components_workflows_WorkflowActivity.051")} <NeryaGlyph name="arrowRight" size={16} /></button></div>
  </>}
    activity={<>    <WorkflowRunCanvas key={`${strategyId}:${item.kind}:${item.id}`} invocation={{
      id: item.id, kind: item.kind, title: item.kind === "agent" ? t("copy.components_workflows_WorkflowActivity.016") : t("copy.components_workflows_WorkflowActivity.017"), status: state.label,
      input: detail.context_snapshot ?? task.inputs,
      output: item.kind === "agent" ? reply || task.error || task.decision : task.outputs ?? task.error,
      calls: [...calls.map((call) => ({ ...call, status: memberState(call.status) })), ...members.map((member) => ({ id: `member:${member.id}`, name: roleNames[member.name] || member.name, kind: "agent" as const, status: memberState(member.state), output: member.error ?? member.output }))],
    }} prompt={item.kind === "agent" ? detail.prompt : undefined} reply={item.kind === "agent" ? reply || undefined : undefined} messages={recorded.messages} partial={!!detail.prompt_error || !!detail.context_snapshot_error || !!asObject(detail.recorded_turn).partial} />
    {members.length > 0 && <section className={styles.parallel} data-testid="workflow-parallel-run"><div className={styles.responseTitle}>{members.length > 1 ? t("copy.components_workflows_WorkflowActivity.031") : t("copy.components_workflows_WorkflowActivity.032")}</div><div className={styles.memberGrid}>{members.map((member) => <details key={member.name} className={styles.member} data-member={member.name} data-state={member.state}><summary><strong>{roleNames[member.name] || member.name}</strong><span>{memberState(member.state)}{member.elapsed !== undefined ? ` · ${durationText(member.elapsed, t)}` : ""}</span></summary>{member.error != null && <Evidence value={member.error} />}{member.output != null && <Evidence value={member.output} />}</details>)}</div></section>}
    {tools.length > 0 ? <details className={styles.steps} open={isRunning} data-testid="workflow-run-steps"><summary>{t("copy.components_workflows_WorkflowActivity.037", { value0: tools.length })}{tools.some((tool) => tool.state === "error") ? ` · ${t("copy.components_workflows_WorkflowActivity.038")}` : ""}</summary><div className={styles.tools}>{tools.map((tool) => <details key={tool.id} className={styles.tool} data-state={tool.state}><summary><span>{toolTitles[tool.name] || tool.name}</span><small>{tool.state === "error" ? t("copy.components_workflows_WorkflowActivity.039") : tool.state === "running" ? t("copy.components_workflows_WorkflowActivity.040") : t("copy.components_workflows_WorkflowActivity.041")}{tool.elapsed !== undefined ? ` · ${durationText(tool.elapsed, t)}` : ""}</small></summary><p className={styles.muted}>{tool.name}</p><h5>{t("copy.components_workflows_WorkflowActivity.042")}</h5><Evidence value={tool.input} /><h5>{t("copy.components_workflows_WorkflowActivity.043")}</h5><Evidence value={tool.output} /></details>)}</div></details> : null}
</>}
    provenance={<>    <details className={styles.section}><summary>{t("copy.components_workflows_WorkflowActivity.044")}</summary><div className={styles.recordBody}>
      {item.kind === "agent" && <details><summary>{t("copy.components_workflows_WorkflowActivity.045")}</summary><Evidence value={detail.prompt} />{detail.prompt_error != null && <p className={styles.error}>{String(detail.prompt_error)}</p>}</details>}
      <details data-testid="workflow-context-snapshot"><summary>{t("copy.components_workflows_WorkflowActivity.046")}</summary><Evidence value={detail.context_snapshot ?? task.inputs} />{detail.context_snapshot_error != null && <p className={styles.error}>{String(detail.context_snapshot_error)}</p>}</details>
      <details><summary>{t("copy.components_workflows_WorkflowActivity.047")}</summary><Evidence value={task.metadata} /></details>
      <details><summary>{t("copy.components_workflows_WorkflowActivity.048")}</summary><pre className={styles.raw}>{JSON.stringify({ ...task, task: undefined }, null, 2)}</pre></details>
      {typeof task.iterations === "number" && <span className={styles.muted}>{t("copy.components_workflows_WorkflowActivity.049")} · {task.iterations}</span>}
    </div></details>
</>}
  /></div>;
}

export function WorkflowActivity({ strategyId, proposalId, onEdit, roleNames = {} }: { strategyId: string; proposalId?: string | null; onEdit: (kind: "agent" | "script") => void; roleNames?: Record<string, string> }) {
  const t = useWorkflowText();
  const wx = useTranslations("workflowExperience");
  const [showHistory, setShowHistory] = useState(!proposalId);
  const [live, setLive] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [items, setItems] = useState<Activity[]>([]);
  const [selected, setSelected] = useState("");
  const [limit, setLimit] = useState(100);
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get("workflow_log") === "strategy") { setSelected(params.get("workflow_run") || ""); setShowHistory(true); }
  }, []);
  function chooseInvocation(id: string) { setSelected(id); rememberInvocation(id, "strategy"); }
  const [filter, setFilter] = useState("all");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [updated, setUpdated] = useState("");
  useEffect(() => {
    if (!showHistory) return;
    let disposed = false; let timer: ReturnType<typeof setTimeout>; let active: AbortController | undefined;
    async function poll() {
      if (disposed) return;
      if (document.hidden) { timer = setTimeout(poll, 5000); return; }
      active = new AbortController(); const timeout = setTimeout(() => active?.abort(), 15000);
      if (!disposed) setLoading(true);
      try {
        const suffix = `?strategy_id=${encodeURIComponent(strategyId)}&limit=${limit}`;
        const [runs, tasks] = await Promise.all([callApi<Facts>(`/strategies/runtime/runs${suffix}`, { signal: active.signal }), callApi<Facts>(`/strategies/runtime/agent_tasks${suffix}`, { signal: active.signal })]);
        if (runs.ok === false || tasks.ok === false) throw new Error(String(runs.error || tasks.error));
        if (!disposed) {
          const next = activities(runs.runs, tasks.tasks).filter(row=>row.kind!=="agent"||!row.id.startsWith("run_"));
          setItems(next);
          setError(""); setUpdated(new Date().toLocaleTimeString());
        }
      } catch (reason) { if (!disposed) setError(String(reason)); }
      finally { clearTimeout(timeout); if (!disposed) { setLoading(false); if (live) timer = setTimeout(poll, 5000); } }
    }
    void poll();
    return () => { disposed = true; clearTimeout(timer); active?.abort(); };
  }, [strategyId, showHistory, live, refresh, limit]);
  const filtered = useMemo(() => items.filter((item) => filter === "all" || filter === item.kind || filter === "error" && activityState(item, t).tone === "error"), [items, filter, t]);
  const item = selectedInvocation(filtered, selected, (row) => `${row.kind}:${row.id}`);
  useEffect(() => { if (!selected && item) setSelected(`${item.kind}:${item.id}`); }, [selected, item]);
  return <section className={styles.root} data-testid="workflow-activity">
    {!proposalId&&<TaskRunHistory taskKind="strategy_agent" taskId={strategyId}/>}
    {!proposalId&&<TaskFinancialGrants taskKind="strategy_agent" taskId={strategyId}/>}
    {!showHistory ? <div className={styles.empty}><h3>{t("copy.components_workflows_WorkflowActivity.052")}</h3><button type="button" onClick={() => setShowHistory(true)}>{t("copy.components_workflows_WorkflowActivity.053")} <NeryaGlyph name="arrowRight" size={16} /></button></div> : <>
      <div className={styles.controls}><ChoiceSelect aria-label={t("copy.components_workflows_WorkflowActivity.054")} value={filter} onValueChange={(choiceValue) => { setFilter(choiceValue); setSelected(""); rememberInvocation("", "strategy"); }}><option value="all">{t("copy.components_workflows_WorkflowActivity.055")}</option><option value="script">{t("copy.components_workflows_WorkflowActivity.056")}</option><option value="agent">{t("copy.components_workflows_WorkflowActivity.071")}</option><option value="error">{t("copy.components_workflows_WorkflowActivity.057")}</option></ChoiceSelect><ChoiceSelect className={styles.runPicker} aria-label={t("copy.components_workflows_WorkflowActivity.058")} value={item ? `${item.kind}:${item.id}` : ""} disabled={!filtered.length} onValueChange={chooseInvocation}>{!filtered.length && <option value="">{t("copy.components_workflows_WorkflowActivity.059")}</option>}{filtered.map((row) => <option key={`${row.kind}:${row.id}`} value={`${row.kind}:${row.id}`}>{row.ts && !Number.isNaN(Date.parse(row.ts)) ? new Date(row.ts).toLocaleString() : t("copy.components_workflows_WorkflowActivity.060")} · {row.kind === "agent" ? t("copy.components_workflows_WorkflowActivity.071") : t("copy.components_workflows_WorkflowActivity.061")} · {activityState(row, t).label} · {row.id.slice(-6)}</option>)}</ChoiceSelect><span className={styles.muted} title={t("copy.components_workflows_WorkflowActivity.062")}>{updated ? t("copy.components_workflows_WorkflowActivity.063", { value0: updated }) : ""}</span><span className={styles.spacer} /><label><input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />{t("copy.components_workflows_WorkflowActivity.064")}</label><button type="button" disabled={loading} aria-label={t("copy.components_workflows_WorkflowActivity.065")} onClick={() => setRefresh((n) => n + 1)}><NeryaGlyph name="refresh" size={18} /></button></div>
      {proposalId && <p className={styles.notice}>{t("copy.components_workflows_WorkflowActivity.066")}</p>}
      {error && <details className={styles.error} open role="alert"><summary>{t("copy.components_workflows_WorkflowActivity.067")}</summary><p>{error}</p></details>}
      {!!filtered.length && <div className={replayStyles.records} aria-label={wx("selectInvocation")}>
        {filtered.map((row) => <button type="button" className={replayStyles.record} key={`${row.kind}:${row.id}`} aria-pressed={item?.id === row.id && item?.kind === row.kind} onClick={() => chooseInvocation(`${row.kind}:${row.id}`)}><strong>{row.kind === "agent" ? "Agent" : t("copy.components_workflows_WorkflowActivity.061")} · {activityState(row, t).label}</strong><small>{row.ts ? new Date(row.ts).toLocaleString() : row.id}</small><small>{row.id}</small></button>)}
      </div>}
      {limit < 500 && <button type="button" disabled={loading} onClick={() => setLimit((n) => Math.min(500, n + 100))}>{wx("loadMore")}</button>}
      {!!selected && !item && <p role="status">{wx("noSelection")}</p>}
      <div className={styles.layout}>
        {item ? <RunDetails key={`${item.kind}:${item.id}`} strategyId={strategyId} item={item} live={live} onEdit={onEdit} roleNames={roleNames} /> : <div className={styles.empty}>{loading && !updated ? t("copy.components_workflows_WorkflowActivity.068") : error ? t("copy.components_workflows_WorkflowActivity.069") : t("copy.components_workflows_WorkflowActivity.070")}</div>}
      </div>
    </>}
  </section>;
}
