import { defineConfig } from "@playwright/test";
import base from "./playwright.ui.config";

export default defineConfig({
  ...base,
  testMatch: "workflow-upgrade*.spec.ts",
  timeout: 60_000,
  outputDir: "../../ui-review/workflow-upgrade-20261005/browser-results",
  reporter: [["list"], ["json", { outputFile: "../../ui-review/workflow-upgrade-20261005/browser-results.json" }]],
  use: { ...base.use, baseURL: "http://127.0.0.1:3193" },
  webServer: {
    command: "node ../node_modules/next/dist/bin/next dev --hostname 127.0.0.1 --port 3193",
    url: "http://127.0.0.1:3193", timeout: 120_000, reuseExistingServer: false,
    env: { NERYA_UI_DIST_DIR: ".next-workflow-upgrade", NERYA_UI_TSCONFIG: "tsconfig.workflow-upgrade.json", NERYA_API: "http://127.0.0.1:1", NERYA_E2E: "1" },
  },
});
