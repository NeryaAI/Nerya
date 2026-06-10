// One-shot visual walkthrough: screenshots every main dashboard route
// in dark + light mode so a human (or agent) can eyeball the result of
// the styling/copy passes. Not a test — no assertions beyond console
// error collection.
import { chromium } from "@playwright/test";
import fs from "node:fs";

const BASE = process.env.WALK_BASE || "http://127.0.0.1:3081";
const OUT = process.env.WALK_OUT || "screenshots-walkthrough";
const ROUTES = [
  ["dashboard", "/dashboard"],
  ["chat", "/chat"],
  ["portfolio", "/portfolio"],
  ["orders", "/orders"],
  ["strategies", "/strategies"],
  ["agents", "/agents"],
  ["tasks", "/tasks"],
  ["workflows", "/workflows"],
  ["inbox", "/inbox"],
  ["accounts", "/accounts"],
  ["skills", "/skills"],
  ["memory", "/memory"],
  ["self-evolution", "/self-evolution"],
  ["incidents", "/incidents"],
  ["settings", "/settings"],
  ["setup", "/setup"],
  ["gateway", "/gateway"],
  ["env-vault", "/env-vault"],
  ["web-search", "/web-search"],
  ["browsers", "/browsers"],
  ["advanced", "/advanced"],
];

fs.mkdirSync(OUT, { recursive: true });
const browser = await chromium.launch();
const errors = [];

for (const theme of ["dark", "light"]) {
  const ctx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    locale: "zh-CN",
  });
  const page = await ctx.newPage();
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(`[${theme}] ${page.url()} :: ${msg.text().slice(0, 200)}`);
  });
  // Persist theme choice the same way the app's settings module does.
  await page.addInitScript((t) => {
    try {
      window.localStorage.setItem(
        "nerya.ui_settings.v1",
        JSON.stringify({ darkMode: t })
      );
    } catch {}
  }, theme);

  for (const [name, route] of ROUTES) {
    try {
      await page.goto(BASE + route, { waitUntil: "networkidle", timeout: 45000 });
      await page.waitForTimeout(1200);
      await page.screenshot({ path: `${OUT}/${name}-${theme}.png`, fullPage: false });
      process.stdout.write(`ok ${name}-${theme}\n`);
    } catch (e) {
      process.stdout.write(`FAIL ${name}-${theme}: ${String(e).slice(0, 120)}\n`);
    }
  }
  await ctx.close();
}

await browser.close();
if (errors.length) {
  fs.writeFileSync(`${OUT}/console-errors.txt`, errors.join("\n"));
  process.stdout.write(`console errors: ${errors.length} (saved)\n`);
} else {
  process.stdout.write("console errors: 0\n");
}
