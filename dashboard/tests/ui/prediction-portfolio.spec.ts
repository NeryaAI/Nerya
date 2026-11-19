import { test, expect, type Page } from "@playwright/test";

const market = `POLYMARKET:${"6".repeat(77)}`;
const now = Date.now()/1000;
const position = { account_id: "prediction-fixture", position_id: "outcome-fixture", strategy_id: "__merged__",
  shares: [{ strategy_id: "event-strategy", size_base: 10 }],
  market, side: "long", size_base: 10, avg_entry_price: .4, mark_price: 0, market_value_usd: 0,
  notional_usd: 0, unrealized_pnl_usd: -4, realized_pnl_usd: 0,
  instrument: { type: "prediction_outcome", label: "Will this long event description remain readable on a narrow screen?",
    outcome: "No", quantity_unit: "shares", price_unit: "PUSD", settlement_status: "resolved", data_status: "available" } };

async function fixture(page: Page, locale: string, state: "normal" | "empty" | "error" = "normal", spot = false) {
  const errors: string[] = [], calls: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(language => localStorage.setItem("nerya.ui_settings.v1", JSON.stringify({ language, darkMode: "dark" })), locale);
  const rows = spot ? [{ ...position, market: "BTCUSDT", instrument: undefined }] : state === "normal" ? [position, { ...position, position_id: "unknown", market: `${market}1`, mark_price: null,
    market_value_usd: null, notional_usd: null, unrealized_pnl_usd: null,
    instrument: { type: "prediction_outcome", data_status: "unavailable" } }] : [];
  const account = { id: "prediction-fixture", kind: spot ? "cex" : "prediction_market", pnl_scope: spot ? "account" : "strategy", mode: "shadow", equity_usd: 250,
    cash_usd: 125, available_usd: 100, locked_usd: 25, trade_count: 1, fees_paid_usd: null,
    fees_status: "unknown", live_trading_enabled: false, positions: Object.fromEntries(rows.map(row => [row.market, row])) };
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api\/proxy/, "");
    calls.push(path);
    let body: unknown = { ok: true, data: {}, accounts: [], items: [], positions: [], orders: [], reports: [],
      events: [], approvals: [], connectors: [], points: [], protections: [], active_executors: [] };
    if (path === "/auth/status") body = { ok: true, local_access: true, password_configured: true };
    else if (path === "/portfolio/summary") body = { accounts: [account], totals: { cash_usd: 125, equity_usd: 250 } };
    else if (path === "/portfolio/positions") {
      if (state === "error") return route.fulfill({ status: 503, json: { error: "Positions unavailable" } });
      body = { positions: rows };
    } else if (path === "/accounts/get") body = { ok: true, account: {
      profile: { id: account.id, label: "Prediction fixture", venue: "polymarket", kind: "prediction_market", mode: "shadow",
        base_currency: "PUSD", status: "active", credentials: {}, permissions: {}, limits: {}, metadata: {} },
      snapshot: { health: "ok", ts: now, total_usd: 250, available_usd: 100, free_usd: 100 },
      open_positions: rows, open_position_count: rows.length, protections: [], active_executors: [], bound_strategies: [],
    } };
    else if (path === "/accounts/equity_curve") body = { ok: true, account_id: account.id, points: [] };
    else if (path === "/portfolio/health") body = { accounts: [], totals: {} };
    else if (path === "/operator/nav") body = { ok: true, data: { primary: [], advanced: [] }, primary: [], advanced: [] };
    else if (path === "/workspace") body = { root: "fixture", live_trading_enabled: false, kill_switch: false };
    else if (path === "/health") body = { status: "ok" };
    await route.fulfill({ status: 200, json: body });
  });
  return { errors, calls };
}

for (const locale of ["en", "zh"]) for (const width of [1440, 390]) {
  test(`prediction holdings and cash ${locale} ${width}`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 1000 });
    const state = await fixture(page, locale);
    await page.goto("/portfolio");
    const desk = page.getByTestId("portfolio-desk");
    await expect(desk.getByTestId("prediction-identity").filter({ hasText: position.instrument.label })).toContainText("No");
    await expect(desk).toContainText(locale === "zh" ? "账户总资产包含账户内全部资产" : "Account value includes all account assets");
    await expect(desk).toContainText(locale === "zh" ? "市场信息暂不可用" : "Market details unavailable");
    const row = desk.getByTestId("position-row").filter({ hasText: position.instrument.label });
    await row.locator("summary").click();
    await expect(row).toContainText(market);
    await expect(row).toContainText("PUSD");
    await expect(row).toContainText("4.00");
    await expect(row).toContainText("event-strategy");
    await expect(row).not.toContainText("__merged__");
    const cash = desk.getByTestId("prediction-cash-details");
    await cash.locator("summary").click();
    await expect(cash).toContainText("100.00"); await expect(cash).toContainText("25.00");
    await expect(cash).toContainText(locale === "zh" ? "费用尚未核实" : "Fees have not been verified");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
    await page.screenshot({ path: info.outputPath(`prediction-${locale}-${width}.png`), fullPage: true });
    await row.scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath(`prediction-position-${locale}-${width}.png`) });
    expect(state.errors).toEqual([]);
    expect(state.calls.filter(path => /submit|cancel|refresh|connect|capture/.test(path))).toEqual([]);
  });
}

for (const state of ["empty", "error"] as const) test(`prediction ${state} is explicit`, async ({ page }) => {
  const observed = await fixture(page, "en", state);
  await page.goto("/portfolio");
  await expect(page.getByTestId("portfolio-desk")).toContainText("250.00");
  await expect(page.getByTestId("portfolio-desk").getByTestId("position-row")).toHaveCount(0);
  expect(observed.errors).toEqual([]);
});

test("account details reuse outcome identity", async ({ page }) => {
  const state = await fixture(page, "en");
  await page.goto("/accounts/prediction-fixture");
  await expect(page.getByTestId("prediction-identity").first()).toContainText(position.instrument.label);
  await expect(page.getByTestId("prediction-identity").first()).toContainText("No");
  expect(state.errors).toEqual([]);
});

test("ordinary positions keep their existing presentation", async ({ page }) => {
  const state = await fixture(page, "en", "normal", true);
  await page.goto("/portfolio");
  const desk = page.getByTestId("portfolio-desk");
  await expect(desk.getByTestId("position-row")).toContainText("BTCUSDT");
  await expect(desk.getByTestId("prediction-identity")).toHaveCount(0);
  await expect(desk.getByTestId("prediction-cash-details")).toHaveCount(0);
  expect(state.errors).toEqual([]);
});
