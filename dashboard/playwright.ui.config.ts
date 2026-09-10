import { defineConfig } from "@playwright/test";

/** UI-only regression: deliberately independent of the runtime E2E global setup.
 * Start a local dashboard preview, then run:
 *   npx --no-install playwright test --config playwright.ui.config.ts
 * API requests are intercepted in the spec; no real runtime is required.
 */
export default defineConfig({
  testDir: "./tests/ui",
  timeout: 40_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  outputDir: "./test-results/ui-usability",
  use: {
    baseURL: process.env.NERYA_UI_BASE_URL || "http://127.0.0.1:3001",
    browserName: "chromium",
    viewport: { width: 1440, height: 900 },
    serviceWorkers: "block",
    contextOptions: { reducedMotion: "reduce" },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
