import type { WorkflowKind, WorkflowNode } from "./workflowTypes";
import { sourceSummary } from "./workflowSources";
import { parseScriptDocumentation } from "./scriptDocumentation";

export type { ResourceTranslator as WorkflowText } from "./i18n";
import type { ResourceTranslator as WorkflowText } from "./i18n";
export const asObject = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const names: Record<string, string> = { "main.py": "copy.workflowTitles.001", "market_inputs.py": "copy.workflowTitles.002", "signals.py": "copy.workflowTitles.003", "risk_rules.py": "copy.workflowTitles.004", "Trading schedule": "copy.workflowTitles.005", "Review schedule": "copy.workflowTitles.006", "Strategy Agent": "copy.workflowTitles.007", "market_analyst": "copy.workflowTitles.008", "risk_critic": "copy.workflowTitles.009", "strategy_tuner": "copy.workflowTitles.010", "Risk & approval gate": "copy.workflowTitles.011", "paper_main": "copy.workflowTitles.012", "Run evidence": "copy.workflowTitles.013", "Change proposal": "copy.workflowTitles.014", "Validation & replay": "copy.workflowTitles.015", "Operator approval": "copy.workflowTitles.016", "Version & apply": "copy.workflowTitles.017", "Observe & learn": "copy.workflowTitles.018" };
export function cardTitle(node: WorkflowNode, t: WorkflowText): string {
  if (node.id === "evidence:review" && node.title === "Run evidence") return t("copy.simpleReview.scriptTitle");
  if (node.id === "agent:tuner" && node.title === "strategy_tuner") return t("copy.simpleReview.agentTitle");
  if (node.kind === "script" && node.content && node.title === node.resource) {
    const title = parseScriptDocumentation(node.content).title;
    if (title) return title;
  }
  const configuredTitle = asObject(asObject(node.config).agent_profile).title;
  if (node.kind === "agent" && node.title === "Strategy Agent" && typeof configuredTitle === "string" && configuredTitle.trim()) return configuredTitle;
  return names[node.title] ? t(names[node.title]) : node.title;
}
const purposes: Record<WorkflowKind, string> = { strategy: "copy.workflowPurposes.001", source: "copy.workflowPurposes.002", script: "copy.workflowPurposes.003", agent: "copy.workflowPurposes.004", scheduler: "copy.workflowPurposes.005", account: "copy.workflowPurposes.006", risk: "copy.workflowPurposes.007", evidence: "copy.workflowPurposes.008", proposal: "copy.workflowPurposes.009", validation: "copy.workflowPurposes.010", approval: "copy.workflowPurposes.011", apply: "copy.workflowPurposes.012", observation: "copy.workflowPurposes.013" };
export function cardPurpose(node: WorkflowNode, t: WorkflowText): string {
  if (node.kind === "script" && node.content) {
    const description = parseScriptDocumentation(node.content).description;
    if (description) return description;
  }
  if (node.description) return node.description;
  if (node.id === "evidence:review") return t("copy.simpleReview.scriptPurpose");
  if (node.id === "agent:tuner") return t("copy.simpleReview.agentPurpose");
  const configuredRole = asObject(asObject(node.config).agent_profile).role;
  if (node.kind === "agent" && typeof configuredRole === "string" && configuredRole.trim()) return configuredRole;
  if (node.kind === "strategy" && asObject(node.config).description) return String(asObject(node.config).description);
  return t(purposes[node.kind]);
}
export function duration(seconds: number, t: WorkflowText): string {
  if (seconds > 0 && seconds % 86400 === 0) return t("copy.lib_workflowPresentation.001", { value0: seconds / 86400 });
  if (seconds > 0 && seconds % 3600 === 0) return t("copy.lib_workflowPresentation.002", { value0: seconds / 3600 });
  if (seconds > 0 && seconds % 60 === 0) return t("copy.lib_workflowPresentation.003", { value0: seconds / 60 });
  return t("copy.lib_workflowPresentation.004", { value0: seconds });
}
export function scheduleSummary(config: Record<string, unknown>, t: WorkflowText): string {
  if (config.type === "interval" && Number(config.every_seconds) > 0) return t("copy.lib_workflowPresentation.005") + duration(Number(config.every_seconds), t);
  const cron = String(config.cron || "");
  const zone = String(config.timezone || "UTC");
  const suffix = ` · ${zone}`;
  const hourly = cron.match(/^0 \*\/(1|2|3|4|6|8|12) \* \* \*$/);
  if (hourly) return t("copy.lib_workflowPresentation.006", { value0: hourly[1] }) + suffix;
  const daily = cron.match(/^(\d{1,2}) (\d{1,2}) \* \* \*$/);
  if (daily) return t("copy.lib_workflowPresentation.007") + `${daily[2].padStart(2, "0")}:${daily[1].padStart(2, "0")}` + suffix;
  return cron ? t("copy.lib_workflowPresentation.008", { value0: cron }) + suffix : t("copy.lib_workflowPresentation.009");
}
export function cardFacts(node: WorkflowNode, t: WorkflowText): string {
  const c = asObject(node.config);
  if (node.status) return stateLabel(node.status, t);
  if (node.presentation?.summary) return node.presentation.summary;
  if (node.id === "evidence:review") return t("copy.simpleReview.scriptFacts", { runs: c.runs ?? 200 });
  if (node.id === "agent:tuner") return t("copy.simpleReview.agentFacts");
  switch (node.kind) {
    case "scheduler": return `${scheduleSummary(c, t)} · ${c.enabled === false ? t("copy.lib_workflowPresentation.010") : c.enabled === true ? t("copy.lib_workflowPresentation.011") : t("copy.lib_workflowPresentation.012")}`;
    case "source": return typeof node.config === "string" ? node.config : sourceSummary(c, t);
    case "risk": return c.allow_direct_order === false ? t("copy.lib_workflowPresentation.013") : c.max_single_order_usd !== undefined ? t("copy.lib_workflowPresentation.014", { value0: c.max_single_order_usd }) : t("copy.lib_workflowPresentation.015");
    case "script": return [node.binding.file || t("copy.lib_workflowPresentation.016"), node.control?.can_stop ? t("copy.lib_workflowPresentation.017") : node.control?.paths?.length ? t("copy.lib_workflowPresentation.018") : ""].filter(Boolean).join(" · ");
    case "agent": {
      if (node.id === "agent:tuner") return t("copy.lib_workflowPresentation.019");
      if (node.execution?.mode === "conditional") return t("copy.lib_workflowPresentation.020");
      if (node.execution?.mode === "parallel") return t("copy.lib_workflowPresentation.021");
      if (node.binding.file) return t("copy.lib_workflowPresentation.022");
      const execution = asObject(c.agent_execution);
      return [execution.capabilities === "custom" || !execution.capabilities && Array.isArray(asObject(c.agent_profile).allowed_tools) && (asObject(c.agent_profile).allowed_tools as unknown[]).length ? t("copy.lib_workflowPresentation.023") : t("copy.lib_workflowPresentation.024"), execution.max_iterations ? t("copy.lib_workflowPresentation.025", { value0: execution.max_iterations }) : t("copy.lib_workflowPresentation.026")].join(" · ");
    }
    case "account": return String(node.config || node.resource);
    case "evidence": return c.runs ? t("copy.lib_workflowPresentation.027", { value0: c.runs }) : t("copy.lib_workflowPresentation.028");
    case "approval": return t("copy.lib_workflowPresentation.029");
    case "apply": return t("copy.lib_workflowPresentation.030");
    case "observation": return t("copy.lib_workflowPresentation.031");
    case "validation": return c.require_backtest ? t("copy.lib_workflowPresentation.032") : t("copy.lib_workflowPresentation.033");
    case "proposal": return t("copy.lib_workflowPresentation.034");
    default: return t("copy.lib_workflowPresentation.035");
  }
}
export function tierLabel(value: string, t: WorkflowText): string {
  return ({ light: t("copy.lib_workflowPresentation.036"), medium: t("copy.lib_workflowPresentation.037"), high: t("copy.lib_workflowPresentation.038") } as Record<string, string>)[value] || value;
}
export function stateLabel(value: string, t: WorkflowText): string {
  return ({ draft: t("copy.lib_workflowPresentation.039"), pending_review: t("copy.lib_workflowPresentation.040"), applied: t("copy.lib_workflowPresentation.041"), approved: t("copy.lib_workflowPresentation.042"), active: t("copy.lib_workflowPresentation.043"), published: t("copy.lib_workflowPresentation.044"), paused: t("copy.lib_workflowPresentation.045") } as Record<string, string>)[value] || value;
}
export function at(object: unknown, path: string[]): unknown {
  return path.reduce<unknown>((value, key) => Object.hasOwn(asObject(value), key) ? asObject(value)[key] : undefined, object);
}
export function withValue(object: Record<string, unknown>, path: string[], value: unknown): Record<string, unknown> {
  const [key, ...rest] = path;
  if (!key || ["__proto__", "constructor", "prototype"].includes(key)) return object;
  return { ...object, [key]: rest.length ? withValue(asObject(object[key]), rest, value) : value };
}
