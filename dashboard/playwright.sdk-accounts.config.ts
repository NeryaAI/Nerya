import { defineConfig } from "@playwright/test";
import { createRequire } from "node:module";
import ui from "./playwright.ui.config";

const next = createRequire(`${process.cwd()}/package.json`).resolve("next/dist/bin/next");
export default defineConfig({
  ...ui,
  testMatch: ["sdk-accounts.spec.ts"],
  outputDir: "./test-results/sdk-account-bridge",
  use: { ...ui.use, baseURL: "http://127.0.0.1:3137" },
  webServer: {
    command: `"${process.execPath}" "${next}" dev --hostname 127.0.0.1 --port 3137`,
    url: "http://127.0.0.1:3137/portfolio", reuseExistingServer: false, timeout: 120000,
    env: { NERYA_UI_DIST_DIR: ".next-sdk-account-review", NERYA_UI_TSCONFIG: "tsconfig.accounts-review.json", NERYA_E2E: "1" },
  },
});
