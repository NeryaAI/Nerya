// Retry pass for routes that poll forever (networkidle never fires):
// use domcontentloaded + a generous settle wait instead.
import { chromium } from "@playwright/test";

const BASE = "http://127.0.0.1:3081";
const OUT = "screenshots-walkthrough";
const JOBS = [
  ["strategies", "/strategies", ["dark", "light"]],
  ["self-evolution", "/self-evolution", ["dark", "light"]],
  ["incidents", "/incidents", ["dark", "light"]],
  ["settings", "/settings", ["light"]],
  ["setup", "/setup", ["light"]],
];

const browser = await chromium.launch();
for (const [name, route, themes] of JOBS) {
  for (const theme of themes) {
    const ctx = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      locale: "zh-CN",
    });
    const page = await ctx.newPage();
    await page.addInitScript((t) => {
      try {
        window.localStorage.setItem(
          "nerya.ui_settings.v1",
          JSON.stringify({ darkMode: t })
        );
      } catch {}
    }, theme);
    try {
      await page.goto(BASE + route, { waitUntil: "domcontentloaded", timeout: 60000 });
      await page.waitForTimeout(5000);
      await page.screenshot({ path: `${OUT}/${name}-${theme}.png` });
      process.stdout.write(`ok ${name}-${theme}\n`);
    } catch (e) {
      process.stdout.write(`FAIL ${name}-${theme}: ${String(e).slice(0, 120)}\n`);
    }
    await ctx.close();
  }
}
await browser.close();
