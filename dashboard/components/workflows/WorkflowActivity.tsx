"use client";

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
import { WorkflowHelp } from "./WorkflowNative";
import styles from "./WorkflowActivity.module.css";

function Evidence({ value }: { value: unknown }) {
  const t = useWorkflowText();
  if (value === undefined || value === null) return <span className={styles.muted}>{t("未记录", "Not recorded")}</span>;
  if (typeof value !== "object") return <p className={styles.text}>{typeof value === "boolean" ? t(value ? "是" : "否", value ? "Yes" : "No") : String(value)}</p>;
  return <dl className={styles.facts}>{Object.entries(value).slice(0, 30).map(([key, item]) => <div className={styles.fact} key={key}><dt>{fieldLabel(key, t("zh", "en") === "zh")}</dt><dd>{item !== null && typeof item === "object" ? <details><summary>{Array.isArray(item) ? t(`${item.length} 项`, `${item.length} items`) : t("详情", "Details")}</summary><pre className={styles.raw}>{JSON.stringify(item, null, 2)}</pre></details> : <Evidence value={item} />}</dd></div>)}{Object.keys(value).length > 30 && <p className={styles.muted}>{t("其余字段见原始记录。", "More fields in the original record.")}</p>}</dl>;
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
        if (!disposed) setDetail(out);
        if (turnId && sessionId) {
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
  const tools = runtimeTools(merged, events);
  const members = parallelMembers(merged, events, detail.team_snapshot);
  const memberState = (value: string) => ({ queued: t("等待开始", "Queued"), running: t("分析中", "Analyzing"), returned: t("已返回", "Returned"), error: t("失败", "Failed"), unknown: t("等待最终记录", "Awaiting final record") }[value] || value);
  const reply = typeof task.final_text === "string" && task.final_text.trim() ? task.final_text : String(asObject(task.decision).text || "");
  const partial = events.filter((event) => event.kind === "message.delta").map((event) => String(event.text || "")).join("").slice(-30000);
  const needsApproval = String(task.stopped_reason || "").includes("approval") || (isRunning && events.some((event) => event.kind === "approval.request"));
  const stamp = item.ts && !Number.isNaN(Date.parse(item.ts)) ? new Date(item.ts).toLocaleString() : t("时间未记录", "Time not recorded");
  const errorMessage = String(asObject(task.error).message || (typeof task.error === "string" ? task.error : "") || (state.tone === "error" ? item.reason : ""));
  const pending = [...tools].reverse().find((tool) => tool.state === "running");
  const chosenPath = asObject(task.metadata).path;
  const selectedRoles = asObject(task.metadata).selected_roles;
  const hasPath = typeof chosenPath === "string" && chosenPath.trim().length > 0;
  const toolTitles: Record<string, string> = { market_data: t("读取行情", "Read market data"), team_run: t("协作分析", "Team analysis"), risk_check: t("检查风险", "Check risk"), portfolio_summary: t("读取账户概况", "Read portfolio"), strategy_history: t("读取策略历史", "Read strategy history") };
  return <article className={styles.detail} data-testid="workflow-run-detail">
    <header className={styles.runHeading}><div><h3>{item.kind === "agent" ? t("Agent 运行", "Agent run") : t("脚本运行", "Script run")}</h3><p>{stamp}{typeof task.duration_ms === "number" ? ` · ${durationText(task.duration_ms, t)}` : ""}</p></div><span className={styles.badge} data-tone={state.tone}>{isRunning ? t("进行中", "Running") : state.label}</span></header>
    {hasPath && <section className={styles.response} data-testid="workflow-selected-path"><div className={styles.responseTitle}>{t("脚本选择的路径", "Path selected by the script")}</div><p>{String(task.reason || chosenPath)}</p>{Array.isArray(selectedRoles) && <p className={styles.muted}>{t("本次预先派发：", "Initial dispatch: ")}{selectedRoles.length ? selectedRoles.map((name) => roleNames[String(name)] || String(name)).join("、") : t("仅协调 Agent", "Coordinator only")}</p>}</section>}
    {needsApproval && <p className={styles.notice}>{t("等待你的确认", "Waiting for your approval")}{sessionId && <Link href={`/chat/${encodeURIComponent(sessionId)}`}>{t("查看请求", "Review request")} →</Link>}</p>}
    {isRunning && <div className={styles.now} role="status"><span className={styles.pulse} /><span>{pending ? `${toolTitles[pending.name] || pending.name}…` : partial ? t("正在整理回复…", "Writing response…") : t("等待 Agent 返回…", "Waiting for Agent…")}</span><WorkflowHelp label={t("运行状态说明", "About this status")}><p>{t("显示最近一次收到的状态。等待时间不能证明完成；连接中断会单独提示。", "Shows the latest recorded status. Elapsed time does not prove completion; connection errors are reported separately.")}</p></WorkflowHelp></div>}
    {error && <details className={styles.error} open><summary>{t("更新失败，保留上次记录", "Refresh failed; previous record retained")}</summary><p>{error}</p></details>}
    {errorMessage && <section className={styles.error}><strong>{t("运行中断", "Run failed")}</strong><p>{errorMessage}</p></section>}
    {state.tone === "warning" && task.stopped_reason != null && <p className={styles.notice}>{t("结束原因：", "Stopped: ")}{String(task.stopped_reason)}</p>}
    {members.length > 0 && <section className={styles.parallel} data-testid="workflow-parallel-run"><div className={styles.responseTitle}>{members.length > 1 ? t("并行分析 → 汇总决策", "Parallel analysis → coordination") : t("角色分析 → 汇总决策", "Role analysis → coordination")}</div><div className={styles.memberGrid}>{members.map((member) => <details key={member.name} className={styles.member} data-member={member.name} data-state={member.state}><summary><strong>{roleNames[member.name] || member.name}</strong><span>{memberState(member.state)}{member.elapsed !== undefined ? ` · ${durationText(member.elapsed, t)}` : ""}</span></summary>{member.error != null && <Evidence value={member.error} />}{member.output != null && <Evidence value={member.output} />}</details>)}</div></section>}
    {item.kind === "agent" && (reply || partial) ? <section className={styles.response} data-testid="workflow-run-response"><div className={styles.responseTitle}>{reply ? t("回复", "Response") : t("生成中", "Writing")}</div><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ children, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer">{children}</a> }}>{reply || partial}</ReactMarkdown></section> : item.kind === "script" && task.outputs != null ? <section className={styles.response}><div className={styles.responseTitle}>{t("结果", "Result")}</div><Evidence value={task.outputs} /></section> : !isRunning && !errorMessage && !hasPath && <p className={styles.text}>{item.reason || t("没有最终回复记录。", "No final response recorded.")}</p>}
    {tools.length > 0 ? <details className={styles.steps} open={isRunning} data-testid="workflow-run-steps"><summary>{t(`${tools.length} 个执行步骤`, `${tools.length} execution steps`)}{tools.some((tool) => tool.state === "error") ? ` · ${t("包含失败", "contains failures")}` : ""}</summary><div className={styles.tools}>{tools.map((tool) => <details key={tool.id} className={styles.tool} data-state={tool.state}><summary><span>{toolTitles[tool.name] || tool.name}</span><small>{tool.state === "error" ? t("失败", "Failed") : tool.state === "running" ? t("执行中", "Running") : t("已返回", "Returned")}{tool.elapsed !== undefined ? ` · ${durationText(tool.elapsed, t)}` : ""}</small></summary><p className={styles.muted}>{tool.name}</p><h5>{t("输入", "Input")}</h5><Evidence value={tool.input} /><h5>{t("返回", "Result")}</h5><Evidence value={tool.output} /></details>)}</div></details> : null}
    <details className={styles.section}><summary>{t("任务与原始记录", "Task & original record")}</summary><div className={styles.recordBody}>
      {item.kind === "agent" && <details><summary>{t("任务指令", "Task instructions")}</summary><Evidence value={detail.prompt} />{detail.prompt_error != null && <p className={styles.error}>{String(detail.prompt_error)}</p>}</details>}
      <details data-testid="workflow-context-snapshot"><summary>{t("实际输入数据", "Actual input data")}</summary><Evidence value={detail.context_snapshot ?? task.inputs} />{detail.context_snapshot_error != null && <p className={styles.error}>{String(detail.context_snapshot_error)}</p>}</details>
      <details><summary>{t("运行配置与来源", "Execution policy & provenance")}</summary><Evidence value={task.metadata} /></details>
      <details><summary>{t("原始记录（只读）", "Original record (read-only)")}</summary><pre className={styles.raw}>{JSON.stringify({ ...task, task: undefined }, null, 2)}</pre></details>
      {typeof task.iterations === "number" && <span className={styles.muted}>{t("Agent 迭代", "Agent iterations")} · {task.iterations}</span>}
    </div></details>
    <div className={styles.links}>{sessionId && <Link href={`/chat/${encodeURIComponent(sessionId)}`}>{t("原对话", "Conversation")} ↗</Link>}<button type="button" onClick={() => onEdit(item.kind)}>{t("调整配置", "Edit settings")} →</button></div>
  </article>;
}

export function WorkflowActivity({ strategyId, proposalId, onEdit, roleNames = {} }: { strategyId: string; proposalId?: string | null; onEdit: (kind: "agent" | "script") => void; roleNames?: Record<string, string> }) {
  const t = useWorkflowText();
  const [showHistory, setShowHistory] = useState(!proposalId);
  const [live, setLive] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [items, setItems] = useState<Activity[]>([]);
  const [selected, setSelected] = useState("");
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
        const suffix = `?strategy_id=${encodeURIComponent(strategyId)}&limit=100`;
        const [runs, tasks] = await Promise.all([callApi<Facts>(`/strategies/runtime/runs${suffix}`, { signal: active.signal }), callApi<Facts>(`/strategies/runtime/agent_tasks${suffix}`, { signal: active.signal })]);
        if (runs.ok === false || tasks.ok === false) throw new Error(String(runs.error || tasks.error));
        if (!disposed) { setItems(activities(runs.runs, tasks.tasks)); setError(""); setUpdated(new Date().toLocaleTimeString()); }
      } catch (reason) { if (!disposed) setError(String(reason)); }
      finally { clearTimeout(timeout); if (!disposed) { setLoading(false); if (live) timer = setTimeout(poll, 5000); } }
    }
    void poll();
    return () => { disposed = true; clearTimeout(timer); active?.abort(); };
  }, [strategyId, showHistory, live, refresh]);
  const filtered = useMemo(() => items.filter((item) => filter === "all" || filter === item.kind || filter === "error" && activityState(item, t).tone === "error"), [items, filter, t]);
  const item = filtered.find((row) => `${row.kind}:${row.id}` === selected) || filtered[0];
  return <section className={styles.root} data-testid="workflow-activity">
    {!showHistory ? <div className={styles.empty}><h3>{t("候选版本尚未启用", "This candidate is not active")}</h3><button type="button" onClick={() => setShowHistory(true)}>{t("查看已应用版本的历史", "View applied-version history")} →</button></div> : <>
      <div className={styles.controls}><ChoiceSelect aria-label={t("筛选运行类型", "Filter runs")} value={filter} onValueChange={(choiceValue) => { setFilter(choiceValue); setSelected(""); }}><option value="all">{t("全部运行", "All runs")}</option><option value="script">{t("脚本", "Scripts")}</option><option value="agent">Agent</option><option value="error">{t("失败", "Failures")}</option></ChoiceSelect><ChoiceSelect className={styles.runPicker} aria-label={t("选择运行记录", "Choose a run")} value={item ? `${item.kind}:${item.id}` : ""} disabled={!filtered.length} onValueChange={(choiceValue) => setSelected(choiceValue)}>{!filtered.length && <option value="">{t("暂无记录", "No runs")}</option>}{filtered.map((row) => <option key={`${row.kind}:${row.id}`} value={`${row.kind}:${row.id}`}>{row.ts && !Number.isNaN(Date.parse(row.ts)) ? new Date(row.ts).toLocaleString() : t("时间未记录", "Time unrecorded")} · {row.kind === "agent" ? "Agent" : t("脚本", "Script")} · {activityState(row, t).label} · {row.id.slice(-6)}</option>)}</ChoiceSelect><span className={styles.muted} title={t("最多100次脚本运行与100次Agent任务", "Up to 100 script runs and 100 Agent tasks")}>{updated ? t(`更新于 ${updated}`, `Updated ${updated}`) : ""}</span><span className={styles.spacer} /><label><input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />{t("自动更新", "Auto refresh")}</label><button type="button" disabled={loading} aria-label={t("刷新记录", "Refresh runs")} onClick={() => setRefresh((n) => n + 1)}>↻</button></div>
      {proposalId && <p className={styles.notice}>{t("已应用版本的历史 · 非当前候选结果", "Applied-version history · not this candidate's results")}</p>}
      {error && <details className={styles.error} open role="alert"><summary>{t("无法读取运行记录", "Could not load runs")}</summary><p>{error}</p></details>}
      <div className={styles.layout}>
        {item ? <RunDetails key={`${item.kind}:${item.id}`} strategyId={strategyId} item={item} live={live} onEdit={onEdit} roleNames={roleNames} /> : <div className={styles.empty}>{loading && !updated ? t("读取中…", "Loading…") : error ? t("记录暂时不可用", "Records unavailable") : t("暂无运行记录", "No recorded runs")}</div>}
      </div>
    </>}
  </section>;
}
