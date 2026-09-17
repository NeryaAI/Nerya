import type { WorkflowText } from "./workflowPresentation";
export type SourceConfig = Record<string, unknown>;
export function sourceTypeLabel(type: string, t: WorkflowText): string {
  const labels: Record<string, [string, string]> = { candles: ["K 线", "Candles"], features: ["技术指标", "Indicators"], ticker: ["最新报价", "Latest quote"], news: ["新闻条目", "News"] };
  return labels[type] ? t(...labels[type]) : type;
}
export function sourceDimension(config: SourceConfig, plural: string, singular: string, fallback: string[] = []): string[] {
  const value = config[plural] ?? config[singular];
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : typeof value === "string" && value ? [value] : fallback;
}
export function updateSourceDimension(config: SourceConfig, plural: string, singular: string, values: string[]): SourceConfig {
  const next = { ...config };
  // Untouched legacy fields retain their original shape. Once plural, keep
  // plural even with one value; callers can rely on a stable result envelope.
  if (values.length === 1 && !Object.hasOwn(config, plural)) { next[singular] = values[0]; delete next[plural]; }
  else { next[plural] = values; delete next[singular]; }
  return next;
}
export function sourceErrors(config: SourceConfig, t: WorkflowText): string[] {
  const errors: string[] = [];
  for (const [plural, singular, label] of [["markets", "market", t("品种", "Markets")], ["timeframes", "timeframe", t("周期", "Timeframes")]]) {
    if (Object.hasOwn(config, plural)) {
      const values = config[plural];
      if (!Array.isArray(values) || !values.length || values.some((v) => typeof v !== "string" || !v.trim())) errors.push(`${label}: ${t("至少填写一个有效值", "Enter at least one value")}`);
      else {
        const normalized = values.map((v: string) => v.trim());
        if (new Set(normalized).size !== normalized.length) errors.push(`${label}: ${t("不能重复", "Duplicate values")}`);
        if (Object.hasOwn(config, singular) && (normalized.length !== 1 || normalized[0] !== config[singular])) errors.push(t("单值和多值配置冲突，请保留一种。", "Conflicting scalar and plural settings. Keep one form."));
      }
    } else if (Object.hasOwn(config, singular) && (typeof config[singular] !== "string" || !String(config[singular]).trim())) errors.push(`${label}: ${t("不能为空", "Cannot be blank")}`);
  }
  if (config.limit !== undefined && (typeof config.limit !== "number" || !Number.isInteger(config.limit) || config.limit < 1)) errors.push(t("每组条数必须为正整数", "Rows per series must be a positive integer"));
  if (config.provider === "runtime.market" && config.capability === "ticker" && (config.timeframe !== undefined || config.timeframes !== undefined)) errors.push(t("最新报价没有K线周期，请移除周期配置。", "Ticker snapshots do not have a candle timeframe."));
  if (config.provider === "runtime.news" && ["market", "markets", "timeframe", "timeframes"].some((key) => key in config)) errors.push(t("新闻源使用订阅列表，不使用K线品种和周期。", "News uses subscriptions, not candle markets/timeframes."));
  return errors;
}
export function sourceSummary(config: SourceConfig, t: WorkflowText, fallback: string[] = []): string {
  const markets = sourceDimension(config, "markets", "market", fallback);
  const frames = sourceDimension(config, "timeframes", "timeframe");
  const parts = [markets.length ? t(`${markets.length} 个品种`, `${markets.length} markets`) : "", frames.length ? frames.join(" / ") : String(config.capability || ""), config.limit ? t(`每组 ${config.limit} 条`, `${config.limit} rows each`) : ""];
  return parts.filter(Boolean).join(" · ") || t("按需提供数据", "Configured data input");
}
