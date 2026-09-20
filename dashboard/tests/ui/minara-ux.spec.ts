import { test, expect, type Page } from "@playwright/test";
import { readableTurnSteps } from "../../lib/readableExecution";
import { toolPresentation } from "../../lib/agentConversation";
import type { NativeBlockEnvelope } from "../../lib/chat";

async function startFixture(page: Page, theme: string, locale: string) {
  const calls: string[] = [], errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript(({ theme, locale }) => {
    if (window.top !== window) return;
    localStorage.setItem("nerya.ui_settings.v1", JSON.stringify({ language: locale, darkMode: theme }));
  }, { theme, locale });
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api\/proxy/, "");
    calls.push(`${route.request().method()} ${path}`);
    let body: unknown = { ok: true, data: {}, items: [], count: 0, events: [], approvals: [] };
    if (path === "/auth/status") body = { ok: true, authenticated: true, password_set: true, enabled: true };
    else if (path === "/agent/sessions") body = { sessions: [], has_more: false };
    else if (path === "/agent/session/transcript") body = { ok: true, messages: [], count: 0 };
    else if (path === "/operator/nav") body = { ok: true, data: { primary: [], advanced: [] }, primary: [], advanced: [] };
    else if (path === "/operator/overview") body = { status: "ok", data: { attention: [], counts: {}, accounts: [], strategies: [] } };
    else if (path === "/setup/readiness") body = { status: "ok", data: { checks: [], blocking: [] } };
    else if (path === "/workspace") body = { root: "fixture", live_trading_enabled: false, kill_switch: false };
    else if (path === "/health") body = { status: "ok" };
    else if (path === "/llm/config") body = { ok: true, tiers: [], provider_profiles: [], default_tier: "medium", reasoning_levels: ["none", "low", "medium", "high"] };
    else if (path === "/llm/models") body = { providers: {} };
    else if (path === "/llm/providers" || path === "/llm/catalog") body = { providers: [] };
    else if (path === "/llm/tiers") body = { tiers: [], count: 0 };
    else if (path === "/market/venues") body = { venues: [] };
    else if (path === "/accounts/list") body = { accounts: [], ts: 0 };
    else if (path.includes("strategy/list")) body = { ok: true, strategies: [] };
    await route.fulfill({ json: body }); // Never forward to a real runtime.
  });
  return { calls, errors };
}

for (const route of ["/", "/chat"]) {
  for (const view of [{ width: 1440, theme: "dark", locale: "en" }, { width: 1440, theme: "dark", locale: "zh" }, { width: 1440, theme: "light", locale: "zh" }, { width: 320, theme: "dark", locale: "zh" }]) {
    test(`shared starter ${route} ${view.width}px ${view.theme} ${view.locale}`, async ({ page }, info) => {
      await page.setViewportSize({ width: view.width, height: 900 });
      const state = await startFixture(page, view.theme, view.locale);
      await page.goto(route);
      const start = page.getByTestId("agent-start");
      const input = start.locator("textarea");
      await expect(start).toBeVisible(); await expect(input).toHaveCount(1);
      await expect(start.getByRole("heading", { level: 1 })).toHaveText(view.locale === "zh" ? "今天想研究什么？" : "What would you like to explore?");
      await expect(start.getByTestId("starter-question")).toHaveCount(4);
      const inherited = await start.evaluate((node) => {
        const local = getComputedStyle(node), root = getComputedStyle(document.documentElement);
        return { text: local.getPropertyValue("--text-base"), rootText: root.getPropertyValue("--text-base"), bg: local.getPropertyValue("--bg"), rootBg: root.getPropertyValue("--bg") };
      });
      expect(inherited.text).toBe(inherited.rootText); expect(inherited.bg).toBe(inherited.rootBg);
      const before = await input.getAttribute("placeholder"); expect(before).toBeTruthy();
      expect((await input.boundingBox())!.height).toBeGreaterThanOrEqual(88);
      expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
      await page.screenshot({ path: info.outputPath(`minara-start-${route === "/" ? "home" : "chat"}-${view.width}-${view.theme}.png`) });
      const tabs = start.getByRole("tablist");
      await tabs.getByRole("tab").first().focus(); await page.keyboard.press("End");
      await expect(tabs.getByRole("tab").last()).toBeFocused();
      await expect(start.getByTestId("starter-question")).toHaveCount(3);
      await input.fill("Keep my existing context.");
      const controlsBefore = await start.locator("[data-chat-composer]").getByRole("button").allTextContents();
      const question = start.getByTestId("starter-question").first();
      await question.click();
      const inserted = await input.inputValue(); expect(inserted).toMatch(/^Keep my existing context\.\n\n/);
      await expect(input).toBeFocused();
      await question.click(); await expect(input).toHaveValue(inserted);
      await start.getByRole("button", { name: view.locale === "zh" ? "撤销填入" : "Undo", exact: true }).click();
      await expect(input).toHaveValue("Keep my existing context.");
      await question.click(); await input.fill("I edited the suggestion myself.");
      await expect(start.getByRole("button", { name: view.locale === "zh" ? "撤销填入" : "Undo", exact: true })).toHaveCount(0);
      await tabs.getByRole("tab").first().click();
      await expect(input).toHaveValue("I edited the suggestion myself.");
      expect(await start.locator("[data-chat-composer]").getByRole("button").allTextContents()).toEqual(controlsBefore);
      expect(state.calls.filter((value) => /run_turn|teams\/agents\/(resume|message)|trade|session\/delete/.test(value))).toEqual([]);
      expect(state.errors).toEqual([]);
    });
  }
}

const read: NativeBlockEnvelope = { block: { kind: "tool_use", call_id: "read", action: "read_file", payload: { path: "evidence.md" } } };
const result: NativeBlockEnvelope = { block: { kind: "tool_result", call_id: "read", ok: true, result: { content: "Evidence is available." } } };
test("readable tool projection keeps source inputs and deduplicates delivery", () => {
  const steps = readableTurnSteps([read, result, read, result], 1000)!;
  expect(steps).toHaveLength(1);
  expect(steps[0].data.payload).toEqual({ path: "evidence.md" });
  expect(toolPresentation(steps[0], "completed", false).state).toBe("Done");
});
test("readable projection leaves approvals, charts and specialized tools to existing renderer", () => {
  for (const block of [{ kind: "approval_request" }, { kind: "chart" }, { kind: "tool_use", action: "place_order" }, { kind: "tool_result", action: "create_strategy" }]) {
    expect(readableTurnSteps([read, { block }], 1000)).toBeNull();
  }
});
test("readable projection keeps failures and missing returns distinct without private thinking", () => {
  const steps = readableTurnSteps([read, { block: { kind: "thinking", text: "Private" } }], 1000)!;
  expect(JSON.stringify(steps)).not.toContain("Private");
  expect(toolPresentation(steps[0], "running", false).pending).toBe(true);
  expect(toolPresentation(steps[0], "completed", false).state).toBe("No response recorded");
  const failed = readableTurnSteps([read, { block: { kind: "tool_result", call_id: "read", ok: false, error: "No permission" } }], 1000)!;
  expect(toolPresentation(failed[0], "completed", false).failed).toBe(true);
});
