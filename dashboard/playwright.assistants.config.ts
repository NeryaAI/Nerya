import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/ui", testMatch: ["assistants.spec.ts", "assistant-results.spec.ts", "assistant-chat.spec.ts", "assistant-overview.spec.ts", "assistant-avatar.spec.ts"], workers: 1, retries: 0,
  timeout: 120000, expect: {timeout: 20000},
  use: {baseURL: process.env.NERYA_ASSISTANTS_QA_URL || "http://127.0.0.1:18580", locale: "en-US", viewport: {width: 1440, height: 1000}, trace: "retain-on-failure", screenshot: "only-on-failure"},
  outputDir: process.env.NERYA_ASSISTANTS_QA_ARTIFACTS || "/tmp/nerya-assistants-opendots/browser-artifacts",
  reporter: [["list"]],
});
