import { defineConfig, devices } from "@playwright/test";

if (process.env.NERYA_E2E_ALLOW_FINANCIAL_DATASETS_KEYS !== "1") {
  delete process.env.FINANCIAL_DATASETS_API_KEY;
  delete process.env.NERYA_FINANCIAL_DATASETS_KEYS;
}

/**
 * Playwright config for Nerya dashboard E2E tests.
 *
 * Pre-requisites:
 *   1) Runtime running on :18318  → `python -m nerya.cli.app serve --port 18318 ...`
 *   2) Dashboard running on :3001 → `NERYA_API=http://127.0.0.1:18318 npm run dev:e2e`
 *      (or set `PLAYWRIGHT_AUTOSTART=1` to let webServer below boot both)
 *
 * Run:
 *   npx playwright test                          # all tests
 *   npx playwright test smoke                    # only smoke
 *   npx playwright test C-strategies             # only group C
 *   npx playwright test --headed --workers=1     # debug visually
 */

const PORT = Number(process.env.NERYA_DASHBOARD_PORT ?? process.env.PORT ?? 3001);
const BASE_URL = process.env.NERYA_DASHBOARD_URL ?? `http://127.0.0.1:${PORT}`;
const RUNTIME_PORT = Number(process.env.NERYA_RUNTIME_PORT ?? 18318);
const RUNTIME_URL = process.env.NERYA_API ?? `http://127.0.0.1:${RUNTIME_PORT}`;
const AUTOSTART = process.env.PLAYWRIGHT_AUTOSTART === "1";
const AUTOSTART_WORKSPACE = process.env.NERYA_WORKSPACE?.trim() || "dashboard/.nerya-test-workspace";
const PERMISSION_MODE = process.env.NERYA_PERMISSION_MODE?.trim() || "yolo";

process.env.NERYA_API = RUNTIME_URL;
process.env.PORT = String(PORT);
process.env.NERYA_PERMISSION_MODE = PERMISSION_MODE;
process.env.NEXT_PUBLIC_NERYA_PERMISSION_MODE =
  process.env.NEXT_PUBLIC_NERYA_PERMISSION_MODE?.trim() || PERMISSION_MODE;

export default defineConfig({
  testDir: "./tests/e2e",
  globalSetup: require.resolve("./tests/e2e/global-setup.ts"),
  timeout: 180_000,
  expect: { timeout: 30_000 },
  fullyParallel: false,
  workers: 1,
  // Cold-start LLM tests are flaky — give every case a single retry.
  // Set NERYA_TEST_RETRIES=0 to disable for fast-fail debugging.
  retries: process.env.NERYA_TEST_RETRIES
    ? Number(process.env.NERYA_TEST_RETRIES)
    : process.env.CI
    ? 2
    : 1,
  reporter: process.env.CI
    ? [
        ["github"],
        ["list"],
        ["html", { outputFolder: "playwright-report" }],
        ["json", { outputFile: "test-results/results.json" }],
      ]
    : [
        ["list"],
        ["html", { outputFolder: "playwright-report", open: "never" }],
        ["json", { outputFile: "test-results/results.json" }],
      ],
  outputDir: "./test-results",
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    actionTimeout: 30_000,
    navigationTimeout: 60_000,
    extraHTTPHeaders: {
      "x-nerya-test": "playwright",
    },
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  ...(AUTOSTART && {
    webServer: [
      {
        command:
          `cd .. && python tools/prepare_isolated_test_workspace.py --target "${AUTOSTART_WORKSPACE}" && python -m nerya.cli.app serve --workspace "${AUTOSTART_WORKSPACE}" --port ${RUNTIME_PORT} --no-dashboard`,
        port: RUNTIME_PORT,
        timeout: 120_000,
        reuseExistingServer: !process.env.CI,
      },
      {
        command: "npm run dev:e2e",
        port: PORT,
        timeout: 120_000,
        reuseExistingServer: !process.env.CI,
      },
    ],
  }),
});
