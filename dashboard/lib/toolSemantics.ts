import { copy as i18nCopy } from "./i18n";
/** Presentation of actual tool inputs/results only; never infer trading outcomes. */
export type ToolFamily = "skill" | "shell" | "search" | "read" | "edit" | "plan" | "message" | "backtest" | "strategy" | "market" | "research" | "portfolio" | "agent" | "browser" | "mcp" | "tool";
const text = (value: unknown): string => typeof value === "string" ? value.trim() : "";
const labels: Record<ToolFamily, string> = {
  skill:"copy.lib_toolSemantics.001", shell:"copy.lib_toolSemantics.002", search:"copy.lib_toolSemantics.003",
  read:"copy.lib_toolSemantics.004", edit:"copy.lib_toolSemantics.005", plan:"copy.lib_toolSemantics.006",
  message:"copy.lib_toolSemantics.007", backtest:"copy.lib_toolSemantics.008", strategy:"copy.lib_toolSemantics.009",
  market:"copy.lib_toolSemantics.010", research:"copy.lib_toolSemantics.011", portfolio:"copy.lib_toolSemantics.012",
  agent:"copy.lib_toolSemantics.013", browser:"copy.lib_toolSemantics.014", mcp:"copy.lib_toolSemantics.015", tool:"copy.lib_toolSemantics.016",
};
const actions: Record<string, string> = {
  strategy_view:"copy.lib_toolSemantics.017", strategy_list:"copy.lib_toolSemantics.018",
  strategy_create:"copy.lib_toolSemantics.019", strategy_update:"copy.lib_toolSemantics.020",
  strategy_backtest:"copy.lib_toolSemantics.021",
  strategy_validate:"copy.lib_toolSemantics.022", strategy_run_tick:"copy.lib_toolSemantics.023",
  strategy_run_history:"copy.lib_toolSemantics.024", strategy_history:"copy.lib_toolSemantics.025",
  research_publish_visuals:"copy.lib_toolSemantics.026", web_fetch:"copy.lib_toolSemantics.027",
  skill_view:"copy.lib_toolSemantics.028", script_run:"copy.lib_toolSemantics.029",
  team_run:"copy.lib_toolSemantics.030", subagent_run:"copy.lib_toolSemantics.031",
  read_file:"copy.lib_toolSemantics.032", write_file:"copy.lib_toolSemantics.033", edit_file:"copy.lib_toolSemantics.034",
};

export function toolSemantics(rawName: string, payload: Record<string, unknown>, zh: boolean) {
  const name = rawName.toLowerCase().replace(/^nerya_/, "");
  const skill = text(payload.skill || payload.skill_id);
  const script = text(payload.script || payload.script_path || payload.path);
  // 文件路径只描述目标，不代表动作：读取 backtest.py 不能显示成运行回测。
  const scope = (name === "script_run" ? [name, skill, script].join(" ") : name).toLowerCase();
  const family: ToolFamily = /^(skill|skill_view|skill_load)$/.test(name) ? "skill"
    : /backtest/.test(scope) ? "backtest" : /strategy/.test(scope) ? "strategy"
    : /browser/.test(scope) ? "browser" : /^(team_run|subagent_run|agent_run)$/.test(name) ? "agent"
    : /^(mcp_|mcp\.)/.test(name) ? "mcp" : /market_data|get_candles|ticker|order_book|funding_rate/.test(scope) ? "market"
    : /portfolio|account_list|positions|balances/.test(name) ? "portfolio"
    : (/publish_visual|research/.test(scope) && name === "script_run") || /research_publish/.test(name) ? "research"
    : /todo|plan/.test(name) ? "plan" : /message/.test(name) ? "message"
    : /search|grep|glob/.test(name) ? "search" : /read|fetch|view|list_dir/.test(name) ? "read"
    : /edit|write|patch|create/.test(name) ? "edit" : /shell|bash|exec|command|script_run/.test(name) ? "shell" : "tool";
  const specific = actions[name];
  const title = i18nCopy(zh, (specific && !(name === "script_run" && family !== "shell") ? specific : labels[family]) ?? "");
  const target = text(payload.strategy_id || payload.market || payload.symbol || payload.path || payload.file_path || payload.query || payload.search_query || payload.url || payload.command || payload.cmd || payload.pattern || payload.task || payload.goal || payload.description);
  const subject = family === "mcp" ? [text(payload.namespace || payload.server), text(payload.tool || payload.name)].filter(Boolean).join(" / ") || target || name
    : family === "skill" ? skill || text(payload.name) || name
    : name === "script_run" ? [skill, script].filter(Boolean).join(" / ") || target || name
    : target || skill || name.replace(/_/g," ");
  return { family, title, subject };
}

export function toolResultSummary(value: Record<string, unknown>, zh: boolean): string {
  for (const key of ["summary", "message", "description"]) {
    if (typeof value[key] === "string" && value[key]) return String(value[key]).replace(/\s+/g," ").slice(0,160);
  }
  for (const key of ["candles", "sources", "matches", "items", "results", "rows"]) {
    if (Array.isArray(value[key])) return `${(value[key] as unknown[]).length} ${key === "candles" ? (i18nCopy(zh, "copy.lib_toolSemantics.035")) : key === "sources" ? (i18nCopy(zh, "copy.lib_toolSemantics.036")) : (i18nCopy(zh, "copy.lib_toolSemantics.037"))}`;
  }
  if (typeof value.exit_code === "number") return `${i18nCopy(zh, "copy.lib_toolSemantics.038")} ${value.exit_code}`;
  if (typeof value.count === "number") return `${value.count} ${i18nCopy(zh, "copy.lib_toolSemantics.039")}`;
  return "";
}

export function plainToolOutput(value: unknown, limit = 32000): { text: string; truncated: boolean } {
  const source = typeof value === "string" ? value : "";
  // Strip terminal control sequences, not ordinary Unicode or repeated tokens.
  const cleaned = source.replace(/\u001b\][^\u0007]*(?:\u0007|\u001b\\)/g, "").replace(/\u001b\[[0-?]*[ -/]*[@-~]/g, "");
  return { text: cleaned.slice(-limit), truncated: cleaned.length > limit };
}
