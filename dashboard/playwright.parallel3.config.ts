import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

export default defineConfig({
  ...base,
  fullyParallel: true,
  workers: 3,
  webServer: undefined,
});
