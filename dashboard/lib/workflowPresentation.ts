import type { WorkflowKind, WorkflowNode } from "./workflowTypes";
import { sourceSummary } from "./workflowSources";

export type WorkflowText = (zh: string, en: string) => string;
export const asObject = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const names: Record<string, [string, string]> = {
  "main.py": ["策略入口", "Strategy entry"], "market_inputs.py": ["采集市场数据", "Collect market data"],
  "signals.py": ["计算交易信号", "Calculate signals"], "risk_rules.py": ["过滤风险信号", "Filter risky signals"],
  "Trading schedule": ["策略运行时间", "Strategy schedule"], "Review schedule": ["复盘时间", "Review schedule"],
  "Strategy Agent": ["策略决策 Agent", "Decision Agent"], "market_analyst": ["市场分析 Agent", "Market analyst"],
  "risk_critic": ["风险复核 Agent", "Risk reviewer"], "strategy_tuner": ["复盘优化 Agent", "Review Agent"],
  "Risk & approval gate": ["交易风控", "Trading safeguards"], "paper_main": ["默认模拟账户", "Default paper account"],
  "Run evidence": ["复盘数据", "Review evidence"], "Change proposal": ["改进方案", "Improvement proposal"],
  "Validation & replay": ["验证改进效果", "Validate changes"], "Operator approval": ["人工确认", "Operator approval"],
  "Version & apply": ["应用新版本", "Apply a version"], "Observe & learn": ["观察与反馈", "Observe & learn"],
};
export function cardTitle(node: WorkflowNode, t: WorkflowText): string {
  const configuredTitle = asObject(asObject(node.config).agent_profile).title;
  if (node.kind === "agent" && node.title === "Strategy Agent" && typeof configuredTitle === "string" && configuredTitle.trim()) return configuredTitle;
  return names[node.title] ? t(...names[node.title]) : node.title;
}
const purposes: Record<WorkflowKind, [string, string]> = {
  strategy: ["定义策略目标，连接需要的资源", "Define the objective and connect its resources"],
  source: ["为策略提供数据", "Supply data to the strategy"], script: ["用代码处理数据或执行策略步骤", "Process data or implement a strategy step"],
  agent: ["根据任务和证据进行分析", "Analyze the task using available evidence"], scheduler: ["决定何时触发，不在此立即运行", "Choose when to trigger, without running now"],
  account: ["指定策略使用的账户，不展示密钥", "Choose an account without exposing secrets"], risk: ["限定下单金额与风险边界", "Set order budgets and risk boundaries"],
  evidence: ["选择复盘参考的历史记录", "Choose the history used for review"], proposal: ["设定优化目标和允许修改的范围", "Set objectives and permitted changes"],
  validation: ["新方案应用前必须通过的检查", "Checks required before applying changes"], approval: ["由你决定是否接受改进方案", "You decide whether to accept the proposal"],
  apply: ["审批通过后再应用，保留版本记录", "Apply after approval and keep version history"], observation: ["检查应用效果，为下一次复盘提供依据", "Check outcomes and inform the next review"],
};
export function cardPurpose(node: WorkflowNode, t: WorkflowText): string {
  if (node.description) return node.description;
  const configuredRole = asObject(asObject(node.config).agent_profile).role;
  if (node.kind === "agent" && typeof configuredRole === "string" && configuredRole.trim()) return configuredRole;
  if (node.kind === "strategy" && asObject(node.config).description) return String(asObject(node.config).description);
  return t(...purposes[node.kind]);
}
export function duration(seconds: number, t: WorkflowText): string {
  if (seconds > 0 && seconds % 86400 === 0) return t(`${seconds / 86400} 天`, `${seconds / 86400} days`);
  if (seconds > 0 && seconds % 3600 === 0) return t(`${seconds / 3600} 小时`, `${seconds / 3600} hours`);
  if (seconds > 0 && seconds % 60 === 0) return t(`${seconds / 60} 分钟`, `${seconds / 60} minutes`);
  return t(`${seconds} 秒`, `${seconds} seconds`);
}
export function scheduleSummary(config: Record<string, unknown>, t: WorkflowText): string {
  if (config.type === "interval" && Number(config.every_seconds) > 0) return t("每 ", "Every ") + duration(Number(config.every_seconds), t);
  const cron = String(config.cron || "");
  const zone = String(config.timezone || "UTC");
  const suffix = ` · ${zone}`;
  const hourly = cron.match(/^0 \*\/(1|2|3|4|6|8|12) \* \* \*$/);
  if (hourly) return t(`每 ${hourly[1]} 小时整点`, `Every ${hourly[1]} hours, on the hour`) + suffix;
  const daily = cron.match(/^(\d{1,2}) (\d{1,2}) \* \* \*$/);
  if (daily) return t("每天 ", "Daily ") + `${daily[2].padStart(2, "0")}:${daily[1].padStart(2, "0")}` + suffix;
  return cron ? t(`自定义时间 · ${cron}`, `Custom · ${cron}`) + suffix : t("尚未设置时间", "Schedule not set");
}
export function cardFacts(node: WorkflowNode, t: WorkflowText): string {
  const c = asObject(node.config);
  if (node.status) return stateLabel(node.status, t);
  if (node.presentation?.summary) return node.presentation.summary;
  switch (node.kind) {
    case "scheduler": return `${scheduleSummary(c, t)} · ${c.enabled === false ? t("未启用", "Disabled") : c.enabled === true ? t("已配置启用", "Configured on") : t("未配置", "Not configured")}`;
    case "source": return typeof node.config === "string" ? node.config : sourceSummary(c, t);
    case "risk": return c.allow_direct_order === false ? t("脚本直接下单：禁止", "Direct script orders: blocked") : c.max_single_order_usd !== undefined ? t(`单笔上限 $${c.max_single_order_usd}`, `Order cap $${c.max_single_order_usd}`) : t("按已配置的风控规则", "Configured risk rules");
    case "script": return [node.binding.file || t("自定义脚本", "Custom script"), node.control?.can_stop ? t("可结束本轮", "Can stop this run") : node.control?.paths?.length ? t("按条件派发", "Conditional dispatch") : ""].filter(Boolean).join(" · ");
    case "agent": {
      if (node.id === "agent:tuner") return t("复盘证据分析 · 返回修改提案", "Review evidence · propose changes");
      if (node.execution?.mode === "conditional") return t("脚本选中后运行", "Runs when selected by script");
      if (node.execution?.mode === "parallel") return t("并行分析 · 结果交给协调 Agent", "Parallel · returns to coordinator");
      if (node.binding.file) return t("可编辑角色指令", "Editable role instructions");
      const execution = asObject(c.agent_execution);
      return [execution.capabilities === "custom" || !execution.capabilities && Array.isArray(asObject(c.agent_profile).allowed_tools) && (asObject(c.agent_profile).allowed_tools as unknown[]).length ? t("自定义能力", "Custom capabilities") : t("主 Agent 能力", "Main Agent capabilities"), execution.max_iterations ? t(`${execution.max_iterations} 轮预算`, `${execution.max_iterations} rounds`) : t("继承运行预算", "Inherited budget")].join(" · ");
    }
    case "account": return String(node.config || node.resource);
    case "evidence": return c.runs ? t(`最近 ${c.runs} 次运行`, `Latest ${c.runs} runs`) : t("按已配置的回看范围", "Configured lookback");
    case "approval": return t("始终需要你的确认", "Always requires your approval");
    case "apply": return t("审批后操作", "After approval");
    case "observation": return t("查看实际记录", "View recorded outcomes");
    case "validation": return c.require_backtest ? t("要求回测验证", "Backtest required") : t("自定义验证条件", "Custom checks");
    case "proposal": return t("保存为待审核方案", "Saved for review");
    default: return t("目标与说明", "Objective & description");
  }
}
export function tierLabel(value: string, t: WorkflowText): string {
  return ({ light: t("轻量", "Light"), medium: t("均衡", "Balanced"), high: t("深度", "Deep") } as Record<string, string>)[value] || value;
}
export function stateLabel(value: string, t: WorkflowText): string {
  return ({ draft: t("草稿", "Draft"), pending_review: t("待审核", "Pending review"), applied: t("已应用", "Applied"), approved: t("已审批", "Approved"), active: t("当前版本", "Current"), published: t("当前版本", "Current"), paused: t("已暂停", "Paused") } as Record<string, string>)[value] || value;
}
export function at(object: unknown, path: string[]): unknown {
  return path.reduce<unknown>((value, key) => Object.hasOwn(asObject(value), key) ? asObject(value)[key] : undefined, object);
}
export function withValue(object: Record<string, unknown>, path: string[], value: unknown): Record<string, unknown> {
  const [key, ...rest] = path;
  if (!key || ["__proto__", "constructor", "prototype"].includes(key)) return object;
  return { ...object, [key]: rest.length ? withValue(asObject(object[key]), rest, value) : value };
}
