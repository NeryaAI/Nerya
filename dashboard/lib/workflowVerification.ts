import type { WorkflowText } from "./workflowPresentation";

export type VerificationIssue = { code: string; message: string; where?: string };
export type Verification = {
  ok: boolean; error?: string; schema: string;
  target: { strategy_id: string; proposal_id: string | null; state: string; revision: string; source_revision: string; checked_at: string };
  validation: { ok: boolean; scope: string; blockers: VerificationIssue[]; warnings: VerificationIssue[] };
  replay: { status: string; id?: string; provenance?: Record<string, unknown>; metrics?: Record<string, unknown> };
  sources: Array<Record<string, unknown>>; report_warnings: string[];
  operation: { state: string; installed_schedules: Array<Record<string, unknown>> };
  mode: string; evaluation_mode: string; next_step: string; unverified: string[];
};
export function replayLabel(status: string, t: WorkflowText): string {
  return ({ missing: t("尚未验证", "Not tested"), unbound: t("证据归属未确认", "Unattributed evidence"), stale: t("结果已过期", "Outdated result"), sample: t("仅样例验证", "Sample test only"), failed: t("回放未通过", "Replay failed"), limited: t("覆盖不完整", "Incomplete coverage"), verified: t("历史回放通过", "Historical replay passed") } as Record<string, string>)[status] || t("状态未知", "Unknown");
}
export function verificationSummary(v: Verification, t: WorkflowText): string {
  if (!v.validation.ok) return t("先修复配置，再进行验证。", "Fix the configuration before testing.");
  if (v.replay.status === "missing") return t("配置检查通过，下一步验证实际行为。", "Configuration checked. Next, test the behavior.");
  if (v.replay.status === "stale") return t("代码已经改变，旧回放不能证明这一版。", "The code changed. The old replay does not verify this version.");
  if (v.replay.status === "sample") return t("样例能跑，不代表真实行情下有效。", "The sample runs; effectiveness on historical data is not established.");
  if (v.replay.status === "verified") return t("历史回放已通过，尚未证明未来表现。", "Historical replay passed; future performance is not established.");
  return t("配置已检查，回放证据仍有缺口。", "Configuration checked. Replay evidence still has gaps.");
}
export function verificationPrompt(v: Verification, t: WorkflowText): string {
  const issues = v.validation.blockers.map((issue) => `${issue.where || "配置"}: ${issue.message}`).join("\n");
  return t(
    `帮我把这版策略验证清楚：先修复具体问题，再完成能运行的分支测试和真实数据回放。只允许回放里的模拟成交，不向账户下单，不开启自动调度或实盘；不要为通过验证而改变策略的交易目标。\n\n当前策略：${v.target.strategy_id}\n候选：${v.target.proposal_id || "当前版本"}\n页面检查的源码版本：${v.target.source_revision}\n检查结果：${verificationSummary(v, t)}${issues ? `\n问题：\n${issues}` : ""}\n\n先读回目标文件确认版本；候选存在就继续同一候选。没有真实数据就明确报告缺口，不用样例代替。分别解释配置、分支行为、回放、真实Agent调用各验证到了哪里。观察策略不以交易次数和收益判成功；交易策略要说明手续费、滑点、数据范围、样本外验证与风险。修改策略逻辑只通过内置 strategy_author。`,
    `Verify this strategy version. Fix concrete issues, then complete runnable branch tests and real-data replay. Allow simulated replay fills only, not account orders, active schedules or live mode. Preserve the strategy's intended trading behavior.\n\nStrategy: ${v.target.strategy_id}\nCandidate: ${v.target.proposal_id || "current"}\nSource revision checked in the UI: ${v.target.source_revision}\nFindings: ${verificationSummary(v, t)}${issues ? `\nIssues:\n${issues}` : ""}\n\nRead the target files and confirm this version first; continue the same candidate. Report missing real data without replacing it with samples. Distinguish configuration checks, branch behavior, replay and actual Agent execution. Observation strategies are not judged by trading counts or returns. For trading include costs, slippage, coverage, out-of-sample checks and risks. Use built-in strategy_author for strategy logic changes.`);
}
export function exportVerification(value: Verification): void {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
  const a = document.createElement("a"); a.href = url; a.download = `${value.target.strategy_id}-verification.json`; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
