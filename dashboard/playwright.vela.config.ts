import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "tests/ui", testMatch: "vela-charts.spec.ts",
  timeout: 60000, expect: { timeout: 15000 }, workers: 1, retries: 0,
  outputDir: "test-results/vela", reporter: "list",
  use: { baseURL: "http://127.0.0.1:18680", headless: true, screenshot: "only-on-failure", trace: "retain-on-failure" },
  webServer: { command: "node scripts/local-server.cjs", url: "http://127.0.0.1:18680", reuseExistingServer: false, timeout: 180000,
    env: { PORT: "18680", NERYA_API: "http://127.0.0.1:1", NERYA_E2E: "1", NERYA_E2E_MOCKED_UI: "1", NERYA_UI_DIST_DIR: ".next-vela", NERYA_UI_TSCONFIG: "tsconfig.vela.json" } },
});
