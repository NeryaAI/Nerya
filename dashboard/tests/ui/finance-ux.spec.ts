import { chooseOption } from "./choice-control";
import { test, expect, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { collectPortfolioArtifacts } from "../../lib/portfolioArtifacts";
import type { ChatThread } from "../../lib/chat";
import { finiteNumber, financeMoney, financeNumber, chartTime } from "../../lib/financeDisplay";
import { cleanSeries, chartSummary } from "../../lib/financialChart";
import { portfolioSnapshot } from "../../lib/portfolioSnapshot";
import { positionExposure } from "../../lib/positionExposure";
import type { ChartBlockShape } from "../../lib/chartBlock";

const now = Math.floor(Date.now() / 1000), session = "finance-ui-fixture";
const accounts = [
  { id: "live-research", mode: "live", equity_usd: 84320.25, cash_usd: 21010.10, fees_paid_usd: 126.32, trade_count: 8, live_trading_enabled: false },
  { id: "paper-lab", mode: "paper", equity_usd: 120440.50, cash_usd: 65000, fees_paid_usd: 93.10, trade_count: 12, live_trading_enabled: false },
];
const positions = [
  { account_id: "live-research", position_id: "btc", market: "BTCUSDT", side: "long", size_base: .35, avg_entry_price: 61020.4, mark_price: 62514.8, unrealized_pnl_usd: 523.04, notional_usd: 21880.18, strategy_id: "trend-study" },
  { account_id: "live-research", position_id: "eth", market: "ETHUSDT", side: "short", size_base: 4.5, avg_entry_price: 3310.25, mark_price: 3355.50, unrealized_pnl_usd: -203.63, notional_usd: 15099.75 },
  { account_id: "live-research", position_id: "alt", market: "ALTUSDT", side: "unknown", size_base: 30, avg_entry_price: .00003425, unrealized_pnl_usd: 0 },
  { account_id: "paper-lab", position_id: "sol", market: "SOLUSDT", side: "long", size_base: 120, avg_entry_price: 136.27, mark_price: 140.89, unrealized_pnl_usd: 554.40 },
];
const summary = { accounts: accounts.map((account) => ({ ...account, snapshot: { ts: now - 60 }, positions: Object.fromEntries(positions.filter((p) => p.account_id === account.id).map((p) => [p.market, p])) })), totals: { equity_usd: 204760.75, cash_usd: 86010.10 } };
const candles = Array.from({ length: 72 }, (_, i) => { const open = 61200 + i * 17 + Math.sin(i / 5) * 180, close = open + Math.cos(i / 3) * 60; return { time: now - (72 - i) * 3600, open, close, high: Math.max(open, close) + 48, low: Math.min(open, close) - 42, volume: 120 + (i % 9) * 23 }; });
const chart: ChartBlockShape = { kind: "chart", chart_id: "price-fixture", market: "BTCUSDT", venue: "bybit", interval: "1h", title: "BTCUSDT · 1h", subtitle: "bybit · UI验收样例，非实时行情", chart_kind: "candlestick", path: "inline", source: { skill: "market", action: "candles", as_of: new Date((now - 3600) * 1000).toISOString(), cite_url: "https://example.com/snapshot-source" }, series: [{ type: "candlestick", name: "BTCUSDT", data: candles }], overlays: [{ type: "price_line", price: 61200, title: "Snapshot reference" }], caption: "离线界面测试数据，不是实时行情或交易建议。" };
const snapshotBlock = { kind: "tool_result", action: "portfolio_summary", skill_id: "trading", call_id: "snapshot", ok: true, result: summary };
const run = "20260913T080000";

async function fixture(page: Page, options: { theme?: string; locale?: string; backtest?: boolean; failPositions?: boolean; emptyPositions?: boolean } = {}) {
  const errors: string[] = [], calls: { path: string; body: Record<string, unknown> }[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript(({ theme, locale }) => { if (window.top === window) localStorage.setItem("nerya.ui_settings.v1", JSON.stringify({ language: locale, darkMode: theme })); }, { theme: options.theme || "dark", locale: options.locale || "zh" });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url()), path = url.pathname.replace(/^\/api\/proxy/, "");
    const input = route.request().method() === "POST" ? route.request().postDataJSON() || {} : {};
    calls.push({ path, body: input });
    let body: unknown = { ok: true, data: {}, accounts: [], items: [], total: 0, count: 0, events: [], approvals: [], reports: [], positions: [] };
    const meta = { session_id: session, title: "行情与仓位复盘 · UI验收样例", created_at: new Date(now * 1000).toISOString(), updated_at: new Date(now * 1000).toISOString(), message_count: 2 };
    const member = { id: "finance-reviewer", name: "Risk reviewer", session_id: session, group_id: "review", title: meta.title, state: "completed", attempt: 1, updated_at: now, context: { scope: "subagent", inherited_messages: 2, saved_messages: 6, allowed_skills: ["trading"], model: "fixture" }, output: { summary: "仓位核查已记录，这是离线UI测试数据。" } };
    if (path === "/auth/status") body = { ok: true, authenticated: true, password_set: true, enabled: true };
    else if (path === "/portfolio/summary") body = summary;
    else if (path === "/portfolio/positions") { if (options.failPositions) return route.fulfill({ status: 503, json: { error: "Positions temporarily unavailable" } }); body = { positions: options.emptyPositions ? [] : positions }; }
    else if (path === "/portfolio/health") body = { accounts: [], totals: {} };
    else if (path === "/accounts/equity_curve") {
      const base = input.account_id === "paper-lab" ? 120000 : 82000;
      body = { ok: true, account_id: input.account_id, points: [{ ts: Number(input.since_ts) - 10, nav_usd: 999999 }, ...Array.from({ length: 40 }, (_, i) => ({ ts: Number(input.since_ts) + (now - Number(input.since_ts) - 60) * i / 39, nav_usd: base + i * 51 + Math.sin(i / 4) * 230 }))] };
    } else if (path === "/kill_switch/get") body = { enabled: false, kill_switch: false };
    else if (path === "/agent/sessions") body = { sessions: [meta], has_more: false };
    else if (path === "/agent/session") body = { ...meta, id: session, messages: [] };
    else if (path === "/agent/session/transcript") body = { ok: true, ...meta, messages: [
      { message_id: "query", role: "user", content: "展示行情、账户仓位和风险。以下都是UI验收样例。", ts: meta.created_at },
      { message_id: "answer", role: "assistant", content: "## 复盘记录\n\n行情与仓位已按数据快照整理。此页面内容为离线UI验收样例，非真实账户数据。", ts: meta.updated_at, turn: { reply_text: "## 复盘记录\n\n行情与仓位已按数据快照整理。此页面内容为离线UI验收样例，非真实账户数据。", blocks: options.backtest ? [{ block: { kind: "tool_result", action: "backtest", ok: true, result: { strategy_id: "fixture-strategy", backtest_ts: run } } }] : [{ block: { kind: "tool_use", action: "portfolio_summary", skill_id: "trading", call_id: "snapshot", payload: {} } }, { block: snapshotBlock }, { block: chart }] } },
    ] };
    else if (path === "/teams/agents") body = { ok: true, agents: [member] };
    else if (path === "/teams/agents/get") body = { ok: true, agent: member, messages: [], has_more: false, events: [{ seq: 1, kind: "started", ts: now, data: { attempt: 1 } }, { seq: 2, kind: "tool_use", ts: now, data: { action: "portfolio_summary", call_id: "snapshot", attempt: 1 } }, { seq: 3, kind: "tool_result", ts: now, data: { ...snapshotBlock, attempt: 1 } }] };
    else if (path === "/agent/stream/events") body = { events: [], latest_seq: 0 };
    else if (path === "/operator/nav") body = { ok: true, data: { primary: [], advanced: [] }, primary: [], advanced: [] };
    else if (path === "/operator/overview") body = { status: "ok", data: { attention: [], counts: {}, accounts: [], strategies: [] } };
    else if (path === "/workspace") body = { root: "fixture", live_trading_enabled: false, kill_switch: false };
    else if (path === "/health") body = { status: "ok" };
    else if (path === "/llm/config") body = { ok: true, tiers: [], provider_profiles: [], default_tier: "medium", reasoning_levels: ["none", "low", "medium", "high"] };
    else if (path === "/llm/models") body = { providers: {} };
    else if (path === "/llm/providers" || path === "/llm/catalog") body = { providers: [] };
    else if (path === "/llm/tiers") body = { tiers: [], count: 0 };
    else if (path === "/market/venues") body = { venues: [] };
    else if (path === "/market/candles") body = { market: "BTCUSDT", interval: input.interval, candles: candles.map((c) => ({ ts: c.time, volume: c.volume, open: c.open + 1000, high: c.high + 1000, low: c.low + 1000, close: c.close + 1000 })) };
    else if (path === "/strategy/backtests/chart") body = { ok: true, strategy_id: "fixture-strategy", ts: run, chart: {
      summary_cards: [{ label: "Total return pct", value: 8.37, tone: "positive" }, { label: "Max drawdown pct", value: 4.21, tone: "warning" }, { label: "Sharpe", value: 1.12 }, { label: "Total trades", value: 23 }, { label: "Win rate pct", value: 56.52 }, { label: "Sortino", value: null }],
      panels: [{ id: "equity", title: "策略与基准 · UI验收样例", type: "timeseries", series: [{ kind: "line", name: "Strategy", data: candles.map((c, i) => ({ time: c.time, value: 100 + i * .1 })) }, { kind: "line", name: "Buy & hold", data: candles.map((c, i) => ({ time: c.time, value: 100 + i * .04 })) }] }], tables: [],
    } };
    await route.fulfill({ status: 200, json: body }); // Never forward requests to the real runtime.
  });
  return { errors, calls };
}
async function openVisual(page: Page) {
  await page.goto(`/chat/${session}`);
  await page.getByRole("tablist", { name: "任务工作区", exact: true }).getByRole("tab", { name: "Canvas", exact: true }).click();
  await page.getByRole("tablist", { name: "Canvas 内容", exact: true }).getByRole("tab", { name: "预览", exact: true }).click();
  await chooseOption(page.getByRole("combobox", { name: "预览内容", exact: true }), `file:charts:${session}`);
}
function noTrading(calls: { path: string }[]) { expect(calls.filter((c) => /order.*(create|submit|cancel)|run_turn|kill_switch\/set|reconciliation\/run|teams\/agents\/(resume|message)/.test(c.path))).toEqual([]); }

test("snapshot resources deduplicate delivery and reject failed records", () => {
  const message = { id: "t1", ts: now * 1000, role: "assistant" as const, turn: { blocks: [{ block: snapshotBlock }, { block: snapshotBlock }] } };
  const thread = { id: session, title: "Snapshot", messages: [message] } as ChatThread;
  expect(collectPortfolioArtifacts(thread)).toHaveLength(1);
  const changed = { ...thread, messages: [{ ...message, turn: { blocks: [{ block: { ...snapshotBlock, ok: false } }] } }] };
  expect(collectPortfolioArtifacts(changed)).toHaveLength(0);
  expect(financeNumber(0.00000003425, "en")).toBe("0.00000003425");
});

test("chart presentation controls and CSV retain original observations", async ({ page }) => {
  const state = await fixture(page); await openVisual(page);
  const figure = page.getByTestId("market-chart-workbench").getByTestId("financial-chart");
  await figure.getByRole("button", { name: "走势", exact: true }).click();
  await expect(figure.getByRole("button", { name: "走势", exact: true })).toHaveAttribute("aria-pressed", "true");
  await figure.getByRole("button", { name: "重置视图", exact: true }).click();
  await expect(figure.getByTestId("chart-canvas")).toBeVisible();
  const pending = page.waitForEvent("download");
  await figure.getByRole("button", { name: "导出 CSV", exact: true }).click();
  const csv = await readFile((await (await pending).path())!, "utf8");
  expect(csv).toContain("series,time_utc,value,open,high,low,close,volume");
  expect(csv).toContain(String(candles[0].open)); expect(csv).toContain(String(candles[0].close));
  await figure.getByRole("button", { name: "K线", exact: true }).click();
  await expect(figure.getByTestId("chart-canvas")).toBeVisible();
  expect(state.errors).toEqual([]); noTrading(state.calls);
});

for (const width of [1440, 320]) test(`Canvas positions become a scoped review draft ${width}`, async ({ page }, info) => {
  await page.setViewportSize({ width, height: 1000 }); const state = await fixture(page);
  await page.goto(`/chat/${session}`);
  const input = page.locator('[data-chat-composer="docked"] textarea');
  await input.fill("保留原来的复盘要求");
  await page.getByRole("tablist", { name: "任务工作区", exact: true }).getByRole("tab", { name: "Canvas", exact: true }).click();
  await page.getByRole("button", { name: "查看本次仓位", exact: true }).click();
  const snapshot = page.getByTestId("canvas-portfolio-snapshot");
  await expect(snapshot).toContainText("非实时刷新");
  await chooseOption(snapshot.getByRole("combobox", { name: "快照账户" }), "paper-lab");
  const amounts = await snapshot.getByTestId("portfolio-snapshot").locator(":scope > dl > div > dd").evaluateAll((nodes) => nodes.map((node) => ({
    text: node.textContent, height: node.getBoundingClientRect().height, line: parseFloat(getComputedStyle(node).lineHeight),
    width: node.clientWidth, contentWidth: node.scrollWidth,
  })));
  expect(amounts).toHaveLength(4);
  for (const amount of amounts) { expect(amount.height).toBeLessThanOrEqual(amount.line + 1); expect(amount.contentWidth).toBeLessThanOrEqual(amount.width + 1); }
  expect(amounts[0].text).toContain("120,440.50");
  const position = snapshot.getByTestId("position-row").filter({ hasText: "SOLUSDT" });
  await position.locator("summary").click();
  await page.screenshot({ path: info.outputPath(`canvas-positions-${width}.png`) });
  await position.getByTestId("review-position").click();
  await expect(input).toBeFocused();
  await expect(input).toHaveValue(/^保留原来的复盘要求\n\n/);
  expect(await input.inputValue()).toContain("paper-lab"); expect(await input.inputValue()).toContain("SOLUSDT");
  expect(await input.inputValue()).toContain("不下单");
  expect(await input.inputValue()).not.toContain("live-research");
  expect(state.errors).toEqual([]); noTrading(state.calls);
});

test("child position review targets its own draft without sending", async ({ page }) => {
  const state = await fixture(page); await page.goto(`/chat/${session}`);
  const main = page.locator('[data-chat-composer="docked"] textarea'); await main.fill("主Agent草稿");
  await page.getByRole("tablist", { name: "任务工作区", exact: true }).getByRole("tab", { name: "Agents", exact: true }).click();
  const panel = page.getByTestId("agent-work-panel"), child = panel.locator("textarea:visible");
  await child.fill("成员草稿");
  await panel.locator('[data-turn-section="trace"] > details > summary').click();
  await panel.getByTestId("agent-operation").locator(":scope > summary").click();
  const position = panel.getByTestId("position-row").filter({ hasText: "BTCUSDT" });
  await position.locator("summary").click(); await position.getByTestId("review-position").click();
  await expect(child).toBeFocused(); await expect(child).toHaveValue(/^成员草稿\n\n/);
  const once = await child.inputValue(); await position.getByTestId("review-position").click(); await expect(child).toHaveValue(once);
  await page.getByRole("tablist", { name: "任务工作区", exact: true }).getByRole("tab", { name: "对话", exact: true }).click();
  await expect(main).toHaveValue("主Agent草稿");
  expect(state.errors).toEqual([]); noTrading(state.calls);
});

test("portfolio review prepares a new draft without running a task", async ({ page }) => {
  const state = await fixture(page); await page.goto("/portfolio");
  const position = page.getByTestId("portfolio-desk").getByTestId("position-row").filter({ hasText: "BTCUSDT" });
  await position.locator("summary").click();
  await page.evaluate(() => sessionStorage.setItem("nerya.compose.draft.v2", JSON.stringify({ text: "之前准备的问题", attachments: [], autoSend: false })));
  await position.getByTestId("review-position").click();
  await expect(page).toHaveURL(/\/chat$/);
  const input = page.getByTestId("agent-start").locator("textarea"); await expect(input).toHaveValue(/^之前准备的问题\n\n/);
  expect(await input.inputValue()).toContain("BTCUSDT");
  expect(state.errors).toEqual([]); noTrading(state.calls);
});

test("exposure reports known gross notional without inventing missing estimates", () => {
  const scope = positionExposure(positions.filter((p) => p.account_id === "live-research"));
  expect(scope.known).toBe(2); expect(scope.missing).toBe(1);
  expect(scope.gross).toBeCloseTo(36979.93, 2);
  expect(scope.directions.long).toBeCloseTo(21880.18, 2);
  expect(scope.directions.short).toBeCloseTo(15099.75, 2);
  expect(scope.markets.map((m) => m.market)).toEqual(["BTCUSDT", "ETHUSDT"]);
  const edge = positionExposure([{ account_id: "test", market: "X", notional_usd: 0 }, { account_id: "test", market: "X", notional_usd: -10, side: "short" }, { account_id: "test", market: "Y", size: 100, mark_price: 50 }]);
  expect(edge.gross).toBe(10); expect(edge.known).toBe(2); expect(edge.missing).toBe(1);
  expect(edge.directions.short).toBe(10); expect(positionExposure([]).gross).toBe(0);
});

test("financial numbers preserve absent values, zero, small prices and signs", () => {
  for (const value of [null, undefined, true, false, "", " ", "NaN", Infinity]) expect(finiteNumber(value)).toBeNull();
  expect(finiteNumber("0")).toBe(0); expect(finiteNumber(.00003425)).toBe(.00003425);
  expect(financeMoney(null, "en")).toBe("—"); expect(financeMoney(0, "en", true)).toBe("$0.00");
  expect(financeMoney(-2, "en", true)).toBe("-$2.00");
});
test("chart observations are sorted, deduplicated and never filled with invented values", () => {
  const first = candles[0], second = candles[1];
  const clean = cleanSeries({ type: "candlestick", name: "price", data: [second, first, { ...first, close: first.open, high: first.open + 1, low: first.open - 1 }, { ...second, time: second.time + 1, high: 1 }] });
  expect(clean.data).toHaveLength(2); expect(clean.data?.[0].time).toBe(first.time);
  expect(chartTime(new Date(first.time * 1000).toISOString())).toBe(first.time);
  expect(chartSummary({ ...chart, series: [{ type: "line", name: "empty", data: [] }] }).end).toBeNull();
});
test("portfolio snapshots reject failed tools and unrelated shapes without merging modes", () => {
  expect(portfolioSnapshot(snapshotBlock)).toHaveLength(2);
  expect(portfolioSnapshot({ ...snapshotBlock, ok: false })).toBeNull();
  expect(portfolioSnapshot({ ...snapshotBlock, result: { ok: false, accounts: [] } })).toBeNull();
  expect(portfolioSnapshot({ ...snapshotBlock, action: "place_order" })).toBeNull();
  expect(portfolioSnapshot({ ...snapshotBlock, result: { accounts: [{ id: "missing" }] } })?.[0].positionsKnown).toBe(false);
});

for (const v of [{ width: 1440, theme: "dark" }, { width: 1440, theme: "light" }, { width: 390, theme: "dark" }, { width: 320, theme: "dark" }]) {
  test(`portfolio desk ${v.width} ${v.theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width: v.width, height: 1000 });
    const state = await fixture(page, v);
    await page.goto("/portfolio");
    const desk = page.getByTestId("portfolio-desk");
    await expect(desk.getByTestId("account-metrics")).toContainText("84,320.25");
    await expect(desk.getByTestId("account-metrics")).not.toContainText("204,760.75");
    await expect(desk.getByTestId("chart-canvas")).toBeVisible();
    await expect(desk.getByTestId("position-row")).toHaveCount(3);
    const exposure = desk.getByTestId("position-exposure");
    await expect(exposure).toContainText("36,979.93");
    await expect(exposure).toContainText("1 / 3");
    await exposure.getByRole("button", { name: "筛选 BTCUSDT 仓位", exact: true }).click();
    await expect(desk.getByRole("textbox", { name: "搜索仓位" })).toBeFocused();
    await expect(desk.getByTestId("position-row")).toHaveCount(1);
    await desk.getByRole("button", { name: "清除筛选", exact: true }).click();
    await expect(desk.getByTestId("position-row")).toHaveCount(3);
    const btc = desk.getByTestId("position-row").filter({ hasText: "BTCUSDT" });
    await btc.locator("summary").click(); await expect(btc).toContainText("61,020.4");
    await btc.locator("summary").click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await desk.evaluate((element) => {
      for (let node: HTMLElement | null = element as HTMLElement; node; node = node.parentElement) node.scrollTop = 0;
      window.scrollTo(0, 0);
    });
    await page.mouse.move(10, 10);
    await page.screenshot({ path: info.outputPath(`portfolio-${v.width}-${v.theme}.png`) });
    await chooseOption(desk.getByRole("combobox", { name: "仓位方向" }), "short");
    await expect(desk.getByTestId("position-row")).toHaveCount(1);
    await chooseOption(desk.getByRole("combobox", { name: "仓位方向" }), "all");
    await chooseOption(desk.getByRole("combobox", { name: "仓位排序" }), "loss");
    await expect(desk.getByTestId("position-row").first()).toContainText("ETHUSDT");
    await chooseOption(desk.getByRole("combobox", { name: "查看账户", exact: true }), "paper-lab");
    await expect(desk.getByTestId("account-metrics")).toContainText("120,440.50");
    await expect(desk.getByTestId("position-row")).toHaveCount(1);
    await expect(desk.getByTestId("position-row")).toContainText("SOLUSDT");
    await desk.getByRole("button", { name: "24H", exact: true }).click();
    await expect(desk.getByTestId("chart-canvas")).toBeVisible();
    const request = state.calls.filter((c) => c.path === "/accounts/equity_curve").at(-1)!;
    expect(request.body.account_id).toBe("paper-lab"); expect(Number(request.body.since_ts)).toBeGreaterThan(now - 86410);
    await desk.getByRole("button", { name: "数据", exact: true }).click();
    await expect(desk.getByTestId("chart-data-table")).not.toContainText("999,999");
    expect(state.errors).toEqual([]); noTrading(state.calls);
  });
}
for (const state of ["error", "empty"]) test(`positions ${state} is explicit and does not fall back to stale summary`, async ({ page }) => {
  const ctx = await fixture(page, { failPositions: state === "error", emptyPositions: state === "empty" });
  await page.goto("/portfolio");
  const positions = page.getByTestId("positions-view");
  await expect(positions).toContainText(state === "error" ? "不代表已清仓" : "没有已记录的持仓");
  await expect(positions.getByTestId("position-row")).toHaveCount(0);
  expect(ctx.errors).toEqual([]);
});

test("late account history cannot overwrite the selected account", async ({ page }) => {
  const state = await fixture(page);
  let release: (() => void) | undefined;
  await page.route("**/api/proxy/accounts/equity_curve", async (route) => {
    const data = route.request().postDataJSON();
    if (data.account_id !== "live-research") return route.fallback();
    await new Promise<void>((resolve) => { release = resolve; });
    await route.fulfill({ json: { ok: true, account_id: data.account_id, points: [{ ts: now - 60, nav_usd: 777777 }] } });
  });
  await page.goto("/portfolio");
  await expect.poll(() => Boolean(release)).toBe(true);
  try {
    await chooseOption(page.getByRole("combobox", { name: "查看账户", exact: true }), "paper-lab");
    await expect(page.getByTestId("chart-canvas")).toBeVisible(); release!();
    await page.getByRole("button", { name: "数据", exact: true }).click();
    await expect(page.getByTestId("chart-data-table")).not.toContainText("777,777");
    await expect(page.getByTestId("account-metrics")).toContainText("120,440.50");
    expect(state.errors).toEqual([]);
  } finally { release?.(); }
});

for (const width of [1440, 320]) test(`main and child portfolio snapshots ${width}`, async ({ page }, info) => {
  await page.setViewportSize({ width, height: 1000 }); const state = await fixture(page);
  await page.goto(`/chat/${session}`);
  const process = page.locator('#chat-workspace-panel-conversation [data-turn-section="trace"] > details > summary');
  await process.click();
  const snapshot = page.locator("#chat-workspace-panel-conversation").getByTestId("portfolio-snapshot");
  await expect(snapshot).toBeVisible(); await expect(snapshot).toContainText("非实时刷新");
  await chooseOption(snapshot.getByRole("combobox", { name: "快照账户" }), "paper-lab");
  await expect(snapshot.getByTestId("position-row")).toContainText("SOLUSDT");
  await page.screenshot({ path: info.outputPath(`snapshot-${width}.png`) });
  await page.getByRole("tablist", { name: "任务工作区", exact: true }).getByRole("tab", { name: "Agents", exact: true }).click();
  const panel = page.getByTestId("agent-work-panel");
  await panel.locator('[data-turn-section="trace"] > details > summary').click();
  await panel.getByTestId("agent-operation").locator(":scope > summary").click();
  await expect(panel.getByTestId("portfolio-snapshot")).toBeVisible();
  expect(state.calls.filter((c) => c.path === "/accounts/equity_curve")).toEqual([]);
  expect(state.errors).toEqual([]); noTrading(state.calls);
});

for (const v of [{ width: 1440, theme: "dark" }, { width: 1440, theme: "light" }, { width: 320, theme: "dark" }]) test(`canvas market chart ${v.width} ${v.theme}`, async ({ page }, info) => {
  await page.setViewportSize({ width: v.width, height: 1000 }); const state = await fixture(page, v);
  await openVisual(page);
  const market = page.getByTestId("market-chart-workbench"), figure = market.getByTestId("financial-chart");
  await expect(figure.getByTestId("chart-canvas")).toBeVisible();
  await expect(market.getByTestId("chart-indicators")).not.toHaveAttribute("open");
  await figure.getByTestId("chart-source").locator("summary").click();
  await expect(figure.getByRole("link", { name: "查看原始来源" })).toHaveAttribute("href", "https://example.com/snapshot-source");
  await figure.getByTestId("chart-source").locator("summary").click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  await page.screenshot({ path: info.outputPath(`market-${v.width}-${v.theme}.png`) });
  await figure.getByRole("button", { name: "放大图表" }).click();
  expect((await figure.getByTestId("chart-canvas").boundingBox())!.height).toBe(460);
  await figure.getByRole("button", { name: "还原图表" }).click();
  await figure.getByRole("button", { name: "数据", exact: true }).click(); await expect(figure.getByTestId("chart-data-table")).toContainText("BTCUSDT");
  await figure.getByRole("button", { name: "图表", exact: true }).click();
  await market.getByRole("group", { name: "K线周期" }).getByRole("button", { name: "4h", exact: true }).click();
  await expect(market.getByRole("button", { name: "4h", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(market).toContainText("未复制到新行情区间");
  await market.getByTestId("chart-source").locator("summary").click();
  await expect(market.getByRole("link", { name: "查看原始来源" })).toHaveCount(0);
  expect(state.errors).toEqual([]); noTrading(state.calls);
});

test("failed candle request retains the current chart and period", async ({ page }) => {
  const state = await fixture(page); await openVisual(page);
  await page.route("**/api/proxy/market/candles", (route) => route.fulfill({ status: 503, json: { error: "fixture unavailable" } }));
  const market = page.getByTestId("market-chart-workbench"), last = await market.getByTestId("chart-last-value").innerText();
  await market.getByRole("button", { name: "4h", exact: true }).click();
  await expect(market.getByRole("alert")).toBeVisible();
  await expect(market.getByRole("button", { name: "1h", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(market.getByTestId("chart-last-value")).toHaveText(last);
  expect(state.errors).toEqual([]);
});

for (const width of [1440, 320]) test(`backtest overview and drilldowns ${width}`, async ({ page }, info) => {
  await page.setViewportSize({ width, height: 1000 }); const state = await fixture(page, { backtest: true });
  await openVisual(page);
  const report = page.getByTestId("backtest-report");
  await expect(report).toBeVisible(); await expect(report).toContainText("不是实盘收益");
  await expect(report.getByRole("region", { name: "风险", exact: true })).toContainText("4.21%");
  await expect(report.getByTestId("backtest-metrics")).not.toContainText("null");
  await page.screenshot({ path: info.outputPath(`backtest-${width}.png`) });
  await report.getByRole("tab", { name: "成交", exact: true }).click(); await expect(report).toContainText("未提供成交明细");
  await report.getByRole("tab", { name: "诊断", exact: true }).click(); await expect(report).toContainText("未提供诊断记录");
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  expect(state.errors).toEqual([]); noTrading(state.calls);
});

for (const width of [1440, 320]) test(`choice control keyboard and viewport ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: 900 });
  const state = await fixture(page);
  await page.goto("/portfolio");
  const account = page.getByRole("combobox", { name: "查看账户", exact: true });
  await expect(account).toHaveAttribute("data-value", "live-research");
  await account.focus();
  await account.press("ArrowDown");
  const list = page.getByRole("listbox");
  await expect(list).toBeFocused();
  await list.press("End");
  await list.press("Escape");
  await expect(account).toBeFocused();
  await expect(account).toHaveAttribute("data-value", "live-research");
  await account.press("ArrowDown");
  const box = (await list.boundingBox())!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.x + box.width).toBeLessThanOrEqual(width);
  await list.press("End");
  await list.press("Enter");
  await expect(account).toBeFocused();
  await expect(account).toHaveAttribute("data-value", "paper-lab");
  await expect(page.getByTestId("account-metrics")).toContainText("120,440.50");
  await expect(page.getByTestId("position-row")).toHaveCount(1);
  expect(state.errors).toEqual([]);
  noTrading(state.calls);
});

test("choice search filters long catalogues without changing selection", async ({ page }) => {
  const state = await fixture(page);
  const extra = Array.from({ length: 10 }, (_, i) => ({ ...summary.accounts[1], id: "paper-review-" + i }));
  await page.route("**/api/proxy/portfolio/summary", route => route.fulfill({ json: { ...summary, accounts: [...summary.accounts, ...extra] } }));
  await page.goto("/portfolio");
  const account = page.getByRole("combobox", { name: "查看账户", exact: true });
  await account.click();
  const search = page.getByRole("searchbox", { name: "搜索选项" });
  await expect(search).toBeFocused();
  await search.fill("not-an-account");
  await expect(page.getByText("没有匹配的选项", { exact: true })).toBeVisible();
  await expect(account).toHaveAttribute("data-value", "live-research");
  await search.fill("PAPER-LAB");
  await expect(page.getByRole("option")).toHaveCount(1);
  await search.press("Enter");
  await expect(account).toHaveAttribute("data-value", "paper-lab");
  await expect(account).toBeFocused();
  expect(state.errors).toEqual([]);
  noTrading(state.calls);
});
