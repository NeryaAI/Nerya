import { expect, test, type Page } from "@playwright/test";

async function fixture(page: Page, { zh = false, empty = false } = {}) {
  await page.addInitScript((zh) => {
    localStorage.setItem("nerya.ui_settings.v1", JSON.stringify({ language: zh ? "zh" : "en", darkMode: true, reducedMotion: true }));
  }, zh);
  const now = Date.now() / 1000;
  const observation = {
    schema_version: 1, connector_id: "fixture_base", observation_id: "fixture_observation", as_of: now,
    collected_at: now, usage: "display_only", source: { kind: "sdk", skill_id: "fixture_reader", script: "reader.py", revision: "fixture-revision", evidence_hash: "fixture-hash" },
    balances: [
      { asset_id: "base:usdc", chain: "base", token: "usdc", symbol: "USDC", amount: "1.234567", available: "1.234567", locked: "0", price_usd: "1", value_usd: "1.234567" },
      { asset_id: "base:unknown", chain: "base", token: "unknown", symbol: "UNPRICED", amount: "900719925474099.312345678", available: null, locked: null, price_usd: null, value_usd: null },
    ],
    positions: [{ id: "fixture_lp", kind: "lp", chain: "base", protocol: "Fixture protocol", label: "Fixture LP position", value_usd: "25", unrealized_pnl_usd: null, claimable_rewards_usd: "0.35" }],
    performance: { realized_pnl_usd: "2.50", unrealized_pnl_usd: null, income_usd: "0.20", fees_usd: "0.03", period_start: String(now - 86400), period_end: String(now) },
    coverage: { balances: "observed", positions: "observed", performance: "observed" },
    summary: { priced_balance_value_usd: "1.234567", unpriced_balances: 1, balance_count: 2, position_count: 1 },
  };
  const connection = { connector_id: "fixture_base", label: "Fixture Base wallet", runtime: "python", requirements: [],
    revision: "fixture-revision", skill_id: "fixture_reader", script: "reader.py", phase: "ready", updated_at: now,
    observation, stale: false, live_verified: false, collection_error: "" };
  const state = { fail: false, calls: [] as string[], errors: [] as string[], delay: 0 };
  page.on("pageerror", error => state.errors.push(error.message));
  await page.route("**/api/**", async route => {
    const path = new URL(route.request().url()).pathname.replace("/api/proxy", "");
    state.calls.push(path);
    let body: unknown = { ok: true, items: [], accounts: [], positions: [], strategies: [], reports: [], groups: [], tools: [], events: [], sessions: [] };
    if (path === "/portfolio/sdk_accounts") body = { ok: true, connectors: empty ? [] : [connection], count: empty ? 0 : 1 };
    if (path === "/portfolio/sdk_accounts/refresh") {
      expect(route.request().postDataJSON()).toEqual({ connector_id: "fixture_base" });
      if (state.delay) await new Promise(resolve => setTimeout(resolve, state.delay));
      connection.stale = state.fail;
      connection.collection_error = state.fail ? "sdk_read_credential_required" : "";
      body = state.fail ? { ok: false, error: "sdk_read_credential_required" } : { ok: true, connector: connection };
    }
    if (path === "/portfolio/summary") body = { totals: { equity_usd: 0, available_usd: 0, allocated_usd: 0 }, accounts: [], strategies: [] };
    if (path === "/portfolio/pnl") body = { realized_usd: 0, unrealized_usd: 0, fees_usd: 0, strategies: [] };
    if (path === "/portfolio/equity_curve") body = { points: [], equity_usd: 0 };
    if (path.endsWith("kill_switch")) body = { kill_switch: false, live_trading_enabled: false };
    if (path === "/runtime/info") body = { ok: true, build_id: "fixture-sdk", protocol_version: 1, capabilities: [] };
    if (path === "/auth/status") body = { ok: true, local_access: true, password_configured: true };
    if (path === "/workspace") body = { root: "isolated-sdk-fixture", live_trading_enabled: false, kill_switch: false };
    if (path === "/operator/nav") body = { data: { primary: [], advanced: [], hidden: [] } };
    await route.fulfill({ contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/portfolio");
  await expect(page.getByTestId("sdk-accounts-portfolio")).toBeVisible();
  return state;
}

test("SDK balances, income and rewards retain their units and separate accounting", async ({ page }, info) => {
  const state = await fixture(page);
  const section = page.getByTestId("sdk-account-fixture_base");
  await expect(section.getByText("Data updated", { exact: true })).toBeVisible();
  await expect(section.getByText("900,719,925,474,099.312345678", { exact: true })).toBeVisible();
  await expect(section.getByRole("row", { name: /UNPRICED/ })).toContainText("—");
  await expect(section.getByText("$2.50", { exact: true })).toBeVisible();
  await expect(section.getByText("$0.20", { exact: true })).toBeVisible();
  await expect(section.getByText("$0.35", { exact: true })).toBeVisible();
  await expect(section.getByText("$1.23", { exact: true })).toHaveCount(2);
  await section.scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("sdk-assets-desktop.png") });
  expect(state.errors).toEqual([]);
  expect(state.calls.some(path => /submit|execute|install/.test(path))).toBe(false);
});

test("failed collection keeps old balances, shows stale state and supports recovery", async ({ page }) => {
  const state = await fixture(page);
  const section = page.getByTestId("sdk-account-fixture_base");
  state.fail = true; state.delay = 300;
  await section.getByRole("button", { name: "Update assets", exact: true }).click();
  await expect(section.getByRole("button", { name: "Updating…", exact: true })).toBeDisabled();
  await expect(section.getByText("Data needs refresh", { exact: true })).toBeVisible();
  await expect(section.getByText("900,719,925,474,099.312345678", { exact: true })).toBeVisible();
  state.fail = false; state.delay = 0;
  await section.getByRole("button", { name: "Update assets", exact: true }).click();
  await expect(section.getByText("Data updated", { exact: true })).toBeVisible();
  expect(state.errors).toEqual([]);
});

test("Chinese mobile view contains the asset table without page overflow", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await fixture(page, { zh: true });
  const section = page.getByTestId("sdk-account-fixture_base");
  await expect(section.getByText("数据已更新", { exact: true })).toBeVisible();
  await expect(section.getByRole("table", { name: "代币余额", exact: true })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
  await section.scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("sdk-assets-mobile-zh.png") });
  expect(state.errors).toEqual([]);
});

test("empty sources offer Agent onboarding without a fake zero asset balance", async ({ page }) => {
  const state = await fixture(page, { empty: true });
  const section = page.getByTestId("sdk-accounts-portfolio");
  await expect(section.getByRole("link", { name: "Connect with Agent", exact: true })).toHaveAttribute("href", "/chat");
  await expect(section.getByRole("table")).toHaveCount(0);
  expect(state.errors).toEqual([]);
});
