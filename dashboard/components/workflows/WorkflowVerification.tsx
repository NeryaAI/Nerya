"use client";

import { useEffect, useState } from "react";
import { workflowApi } from "../../lib/workflowApi";
import { asObject } from "../../lib/workflowPresentation";
import { sourceSummary } from "../../lib/workflowSources";
import { replayLabel, verificationSummary, exportVerification, type Verification } from "../../lib/workflowVerification";
import type { WorkflowView } from "../../lib/workflowTypes";
import { useWorkflowText } from "./WorkflowCanvas";
import { WorkflowEditorDialog } from "./WorkflowEditorDialog";
import ui from "./WorkflowNative.module.css";
import styles from "./WorkflowVerification.module.css";

const scalar = (value: unknown, absent: string) => value === undefined || value === null ? absent : String(value);
export function WorkflowVerification({ workflow, onClose, onEdit }: {
  workflow: WorkflowView; onClose: () => void; onEdit: (where: string) => void;
}) {
  const t = useWorkflowText();
  const [result, setResult] = useState<Verification | null>(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let disposed = false;
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 20000);
    setLoading(true); setError(""); setResult(null);
    void workflowApi.check(workflow.strategy_id, workflow.source.proposal_id, workflow.revision, abort.signal).then((value) => {
      if (disposed || abort.signal.aborted) return;
      if (value.target.revision !== workflow.revision) throw new Error("revision_conflict");
      setResult(value);
    }).catch((reason) => { if (disposed) return; setError(abort.signal.aborted ? t("检查超时，请重试。", "Check timed out. Try again.") : String(reason)); }).finally(() => { clearTimeout(timeout); if (!disposed) setLoading(false); });
    return () => { disposed = true; clearTimeout(timeout); abort.abort(); };
  }, [workflow.strategy_id, workflow.source.proposal_id, workflow.revision, refresh]); // parent callbacks deliberately do not retrigger network work
  const m = asObject(result?.replay.metrics), provenance = asObject(result?.replay.provenance);
  const datasets = Array.isArray(provenance.datasets) ? provenance.datasets.map(asObject) : [];
  const absent = t("未记录", "Not recorded");
  const operation = ({ candidate: t("候选未启用", "Candidate is inactive"), not_installed: t("未安装调度", "No schedule installed"), paused: t("调度已暂停", "Schedule paused"), scheduled: t("已配置调度，触发结果见运行记录", "Scheduled; check run history for execution"), unknown: t("调度状态未确认", "Schedule status unknown") } as Record<string, string>)[result?.operation.state || "unknown"];
  return <WorkflowEditorDialog open title={t("验证记录", "Verification records")} onClose={onClose} footer={<><button type="button" className={ui.quietButton} disabled={!result || loading} onClick={() => result && exportVerification(result)}>{t("导出证据", "Export evidence")}</button><span className={ui.spacer} /><button type="button" className={ui.quietButton} disabled={loading} onClick={() => setRefresh((n) => n + 1)}>{t("刷新记录", "Refresh records")}</button><button type="button" className={ui.quietButton} onClick={onClose}>{t("关闭", "Close")}</button></>}>
    <section className={styles.root} data-testid="workflow-verification" aria-busy={loading}>
      <header className={styles.heading}><div><h2>{t("验证记录", "Verification records")}</h2><p>{String(workflow.manifest.title || workflow.strategy_id)} · {workflow.source.proposal_id ? t("当前候选", "This candidate") : t("当前版本", "Current version")}</p></div><button type="button" className={ui.iconButton} aria-label={t("关闭验证", "Close verification")} onClick={onClose}>×</button></header>
      {loading ? <p className={styles.pending} role="status">{t("检查代码与证据，不运行策略…", "Checking code and evidence without running the strategy…")}</p> : error ? <div role="alert" className={styles.error}><h3>{t("检查未完成", "Check incomplete")}</h3><p>{error.includes("revision_conflict") ? t("文件已变化，请关闭此窗口并重新载入策略。", "Files changed. Close this window and reload the strategy.") : error}</p></div> : result && <>
        <p className={styles.lead} role="status">{verificationSummary(result, t)}</p>
        <div className={styles.stages}>
          <div><span className={styles.mark} data-tone={result.validation.ok ? "ok" : "error"}>{result.validation.ok ? "✓" : "!"}</span><div><strong>{t("配置与代码", "Configuration & code")}</strong><small>{t("结构、语法与SDK规则，不执行脚本", "Schema, syntax and SDK rules; no script execution")}</small></div><span>{result.validation.ok ? t("检查通过", "Checked") : t(`${result.validation.blockers.length} 项待修复`, `${result.validation.blockers.length} issues`)}</span></div>
          <div><span className={styles.mark} data-tone={result.replay.status === "verified" ? "ok" : "pending"}>{result.replay.status === "verified" ? "✓" : "·"}</span><div><strong>{result.evaluation_mode === "observation" ? t("历史行为回放", "Historical behavior replay") : t("交易回测", "Trading backtest")}</strong><small>{result.evaluation_mode === "observation" ? t("检查触发与停止，不用收益评价观察策略", "Check triggers and stops, not observer profits") : t("检查模拟成交与成本，不代表实盘收益", "Simulated fills and costs, not live returns")}</small></div><span>{replayLabel(result.replay.status, t)}</span></div>
          <div><span className={styles.mark} data-tone="pending">·</span><div><strong>{t("持续运行", "Ongoing operation")}</strong><small>{operation}</small></div><span>{t("不自动启动", "No auto-start")}</span></div>
        </div>
        {result.validation.blockers.length > 0 && <section className={styles.issues} aria-label={t("需要修复", "Issues to fix")}>{result.validation.blockers.map((issue, i) => <div key={i}><strong>{issue.code}</strong><p>{issue.message}</p>{issue.where && <button type="button" className={ui.quietButton} onClick={() => onEdit(issue.where || "")}>{t("打开", "Open")} {issue.where} →</button>}</div>)}</section>}
        {result.replay.id && <section className={styles.replay}>
          <div className={styles.replayTitle}><h3>{t("最近回放", "Latest replay")}</h3><span>{provenance.data_kind === "sample" ? t("样例数据", "Sample data") : provenance.data_kind === "historical" ? t("历史数据", "Historical data") : t("来源未确认", "Source unconfirmed")} · {scalar(m.verdict, absent)}</span></div>
          <dl><div><dt>{t("请求周期 → 实际周期", "Requested → used timeframe")}</dt><dd>{scalar(m.requested_primary_timeframe, absent)} → {scalar(m.tf, absent)}</dd></div><div><dt>{t("请求天数 / 实际天数", "Requested / actual days")}</dt><dd>{scalar(m.requested_window_days, absent)} / {typeof m.backtest_days === "number" ? m.backtest_days.toFixed(2) : absent}</dd></div><div><dt>{t("数据区间", "Data window")}</dt><dd>{scalar(m.start_utc, absent)} — {scalar(m.end_utc, absent)}</dd></div>{result.evaluation_mode === "observation" ? <div><dt>{t("有效观察或派发 / 跳过", "Observations or dispatches / skips")}</dt><dd>{scalar(asObject(m.replay).observations_or_dispatches, absent)} / {scalar(asObject(asObject(m.replay).status_counts).skip, absent)}</dd></div> : <><div><dt>{t("回测收益 / 最大回撤", "Backtest return / max drawdown")}</dt><dd>{m.total_return_pct === undefined ? absent : `${m.total_return_pct}%`} / {m.max_drawdown_pct === undefined ? absent : `${m.max_drawdown_pct}%`}</dd></div><div><dt>{t("手续费 / 滑点成本", "Fees / slippage costs")}</dt><dd>{scalar(m.total_fees_usd, absent)} / {scalar(m.total_slippage_usd, absent)} USD</dd></div></>}</dl>
          {(result.replay.status !== "verified" || Boolean(m.coverage_message)) && <p className={styles.note}>{result.replay.status === "stale" ? t("此报告来自旧代码，仅供比较。", "This report is for older code, for comparison only.") : result.replay.status === "unbound" ? t("缺少可核对的版本或数据归属，不作为当前版通过证据。", "Version or data attribution is missing; this does not verify the current version.") : provenance.data_kind === "sample" ? t("这次只验证了样例行为；真实历史数据与完整区间仍待验证。", "Only sample behavior was tested. Real history and the complete requested window still need verification.") : scalar(m.coverage_message, t("这份报告不能替代真实数据与分支验证。", "This report does not replace real-data and branch checks."))}</p>}
        </section>}
        <details className={styles.details}><summary>{t("数据范围与专业证据", "Data scope & technical evidence")}</summary>
          <p className={styles.note}>{t("来源配置不是连通性测试；下表的实际数据只来自已保存的回放。", "Source configuration is not a connectivity test. Actual data below comes only from saved replays.")}</p>
          <div className={styles.tableWrap}><table><thead><tr><th>{t("配置的数据源", "Configured source")}</th><th>{t("读取范围", "Read scope")}</th></tr></thead><tbody>{result.sources.map((source, index) => <tr key={String(source.id || index)}><td>{String(source.title || source.id)}<small>{String(source.provider || "")}</small></td><td>{sourceSummary(source, t)}</td></tr>)}</tbody></table></div>
          {datasets.length > 0 && <div className={styles.tableWrap}><table><thead><tr><th>{t("实际品种 / 周期", "Actual market / timeframe")}</th><th>{t("条数", "Rows")}</th><th>{t("数据指纹", "Data fingerprint")}</th></tr></thead><tbody>{datasets.map((d, index) => <tr key={index}><td>{String(d.market)} · {String(d.timeframe)}</td><td>{String(d.rows)}</td><td><code title={String(d.sha256)}>{String(d.sha256 || "").slice(0, 16)}</code></td></tr>)}</tbody></table></div>}
          <dl><div><dt>{t("源码版本", "Source revision")}</dt><dd><code>{result.target.source_revision}</code></dd></div><div><dt>{t("候选标识", "Candidate")}</dt><dd>{result.target.proposal_id || t("当前版本", "Current version")}</dd></div></dl>
          {Object.keys(asObject(provenance.assumptions)).length > 0 && <details className={styles.details}><summary>{t("成本与成交假设", "Cost & fill assumptions")}</summary><pre>{JSON.stringify(provenance.assumptions, null, 2)}</pre></details>}
          <p className={styles.note}>{t("未验证：完整分支覆盖、真实 Agent 分析、样本外表现及持续模拟运行。历史结果不证明未来收益。", "Not verified here: full branch coverage, real Agent analysis, out-of-sample performance or ongoing paper operation. History does not prove future returns.")}</p>
        </details>
        {(result.validation.warnings.length > 0 || result.report_warnings.length > 0) && <details className={styles.details}><summary>{t("检查提醒", "Check notices")} · {result.validation.warnings.length + result.report_warnings.length}</summary>{result.validation.warnings.map((w, i) => <p className={styles.note} key={i}>{w.message}</p>)}{result.report_warnings.map((w, i) => <p className={styles.note} key={i}>{w}</p>)}</details>}
      </>}
    </section>
  </WorkflowEditorDialog>;
}
