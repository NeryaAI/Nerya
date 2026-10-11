import { test, expect, type Page, type Locator } from "@playwright/test";
import { parityFixture } from "./command-fixture";

async function configurePixelChecks(page: Page, canvas2d: boolean) {
  await page.addInitScript(forceCanvas2d => {
    const original = HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.getContext = function(this: HTMLCanvasElement, type, options) {
      if (forceCanvas2d && (type === "webgl" || type === "webgl2")) return null;
      return Reflect.apply(original, this, [type, type === "webgl2" ? { ...options, preserveDrawingBuffer: true } : options]);
    } as typeof original;
  }, canvas2d);
}

async function coloredPixels(plot: Locator, color: "green" | "amber") {
  return plot.evaluate((node, tone) => {
    let count = 0;
    for (const canvas of node.querySelectorAll("canvas")) {
      const context = canvas.getContext("2d");
      let pixels: Uint8Array | Uint8ClampedArray;
      if (context) pixels = context.getImageData(0, 0, canvas.width, canvas.height).data;
      else {
        const gl = canvas.getContext("webgl2");
        if (!gl) continue;
        pixels = new Uint8Array(canvas.width * canvas.height * 4);
        gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
      }
      for (let i = 0; i < pixels.length; i += 4) {
        const [r, g, b, a] = pixels.subarray(i, i + 4);
        if (a > 20 && (tone === "green" ? g > 50 && g > r * 1.4 && g > b * 1.4 : r > 150 && g > 90 && b < 80)) count++;
      }
    }
    return count;
  }, color);
}

for (const mobile of [false, true]) test(`Vela paints artifacts, keeps attribution and releases scrolling (${mobile ? "mobile light" : "desktop dark"})`, async ({ page }, info) => {
  await configurePixelChecks(page, mobile);
  const fixture = await parityFixture(page, { theme: mobile ? "light" : "dark" });
  await page.route("**/api/proxy/auth/status", route => route.fulfill({ json: { ok: true, local_access: true, authenticated: true } }));
  await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 });
  await page.goto("/dev/chart-demo");
  const plots = page.locator('[data-chart-renderer="vela"][data-chart-ready="true"]');
  await expect(plots).toHaveCount(5);
  await expect(plots.first().locator("canvas").first()).toBeVisible();
  await expect(plots.first().getByRole("link", { name: /Vela/i })).toBeVisible();
  const bounds = await plots.first().boundingBox();
  expect(bounds).not.toBeNull();
  await page.mouse.move(bounds!.x + bounds!.width / 2, bounds!.y + bounds!.height / 2);
  await expect(page.getByTestId("chart-crosshair-values").first()).toContainText("ohlc:");
  const before = await plots.first().evaluate(node => {
    for (let parent = node.parentElement; parent; parent = parent.parentElement) {
      if (parent.scrollHeight > parent.clientHeight && /auto|scroll/.test(getComputedStyle(parent).overflowY)) {
        parent.dataset.testScrollTarget = "true"; return parent.scrollTop;
      }
    }
    throw new Error("No scrollable chart ancestor");
  });
  await page.mouse.wheel(0, 260);
  await expect.poll(() => page.locator('[data-test-scroll-target="true"]').evaluate(node => node.scrollTop)).toBeGreaterThan(before);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({ path: info.outputPath(`vela-${mobile ? "mobile" : "desktop"}.png`), fullPage: true });
  expect(fixture.errors).toEqual([]);
});

for (const mobile of [false, true]) test(`Vela consumer lifecycle and backtest selections (${mobile ? "mobile" : "desktop"})`, async ({ page }, info) => {
  await configurePixelChecks(page, mobile);
  const fixture = await parityFixture(page, { theme: mobile ? "light" : "dark" });
  await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 });
  const start = 1720000000;
  await page.route("**/api/proxy/accounts/equity_curve", route => route.fulfill({ json: { ok: true, points: Array.from({ length: 40 }, (_, i) => ({ ts: start + i * 3600, nav_usd: 1000 + i * 5 })) } }));
  await page.route("**/api/proxy/strategy/backtests/chart", route => route.fulfill({ json: {
    ok: true, strategy_id: "vela-fixture", ts: "fixture", chart: { meta: {}, summary_cards: [], tables: [], panels: [{ id: "equity", type: "timeseries", title: "Equity", series: [
      { kind: "line", name: "NAV", data: Array.from({ length: 40 }, (_, i) => ({ time: start + i * 3600, value: 1000 + i * 5 })) },
    ] }] },
  } }));
  await page.goto("/dev/vela-demo");
  const plots = page.locator('[data-chart-renderer="vela"][data-chart-ready="true"]');
  await expect(plots).toHaveCount(9);
  const candle = page.getByTestId("vela-candles");
  for (const mode of ["line", "area", "candlestick"]) {
    await candle.getByRole("button", { name: mode, exact: true }).click();
    await expect(candle.locator('[data-chart-ready="true"] canvas').first()).toBeVisible();
    await expect(candle.getByRole("button", { name: mode, exact: true })).toHaveAttribute("aria-pressed", "true");
  }
  await candle.getByRole("button", { name: "Volume", exact: true }).click();
  await expect(candle.locator('[data-chart-ready="true"]')).toHaveCount(1);
  const executions = page.getByTestId("vela-executions");
  const market = executions.getByTestId("backtest-market-chart");
  await expect(market).toHaveAttribute("data-marker-count", "3");
  await executions.getByTestId("backtest-trade-row").first().click();
  await expect(executions.getByTestId("backtest-execution-detail")).toContainText("TEST/USD");
  await expect(executions.getByTestId("backtest-trade-row").first()).toHaveAttribute("aria-selected", "true");
  await expect(market.locator("canvas").first()).toBeVisible();
  await executions.getByRole("button", { name: "Fit range", exact: true }).click();
  await expect.poll(() => coloredPixels(market, "amber")).toBeGreaterThan(20);
  await page.getByTestId("vela-account").getByRole("button", { name: "7D", exact: true }).click();
  await expect(page.getByTestId("vela-account").locator('[data-chart-ready="true"]')).toHaveCount(1);
  await expect.poll(() => coloredPixels(page.getByTestId("vela-account").locator('[data-chart-renderer="vela"]'), "green")).toBeGreaterThan(100);
  await page.getByTestId("vela-account").screenshot({ path: info.outputPath(`vela-account-${mobile ? "mobile" : "desktop"}.png`) });
  await executions.screenshot({ path: info.outputPath(`vela-executions-${mobile ? "mobile" : "desktop"}.png`) });
  await page.getByTestId("financial-chart").filter({ hasText: "Test baseline" }).screenshot({ path: info.outputPath(`vela-baseline-${mobile ? "mobile" : "desktop"}.png`) });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({ path: info.outputPath(`vela-consumers-${mobile ? "mobile" : "desktop"}.png`), fullPage: true });
  expect(fixture.errors).toEqual([]);
});
