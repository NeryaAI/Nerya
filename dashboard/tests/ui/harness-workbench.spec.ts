import { test, expect, type Page, type TestInfo } from "@playwright/test";
import { mkdir, readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { parityFixture } from "./command-fixture";

// Screenshots exercise the real application with isolated API fixtures. No
// request is forwarded to a runtime, model, wallet or exchange.
const sourceTime = "2026-10-20T08:00:00Z";
async function assistantFixture(page: Page) {
  const assistant = { id: "main", name: "Nerya", role: "Research assistant", instructions: "", avatar: "", enabled: true,
    revision: 1, created_at: 1, updated_at: 1, model_provider: "", model_id: "", model_tier: "",
    research_allowed: true, memory_allowed: true, browser_allowed: true, files_allowed: true, shell_allowed: false,
    tool_policy: { allow_groups: [], deny: [] } };
  await page.route("**/api/proxy/assistants**", route => {
    const path = new URL(route.request().url()).pathname;
    return route.fulfill({ json: path.endsWith("/session") ? { ok: true, assistant, conversation: { conversation_kind: "task" } }
      : path.endsWith("/conversations") ? { ok: true, sessions: [] }
      : { ok: true, assistants: [assistant], channels: [], workspace: "isolated-fixture" } });
  });
}
const report = "# Strategy replay review\n\nThe candidate is saved, but the requested historical coverage has not passed.\n\n## Findings\n\nThe strategy contract passed its checks. The available history covers 302 of the requested 365 days. Keep the same market, timeframe and risk limits while resolving the gap.\n\n## Next step\n\nInspect the missing history before another replay. No orders were submitted and no schedule was enabled.\n\n*Isolated UI test fixture. Not a live account or an investment result.*";
const artifactIndex = {
  created: ["strategies/btc_review/main.py", "reports/btc_review/replay.md"],
  tests_run: [
    { command: "python -m pytest tests/test_strategy_contract.py -q", exit_code: 0, ok: true },
    { command: "python verify_coverage.py --days 365", exit_code: 1, ok: false },
  ],
  unverified_risks: [{ message: "Historical coverage: 302 of 365 requested days. Live execution was not requested." }],
};

async function capture(page: Page, info: TestInfo, name: string) {
  const directory = process.env.NERYA_UI_CAPTURE_DIR;
  const path = directory ? resolve(directory, `${name}.png`) : info.outputPath(`${name}.png`);
  if (directory) await mkdir(directory, { recursive: true });
  await page.screenshot({ path, fullPage: true, animations: "disabled" });
  await info.attach(name, { path, contentType: "image/png" });
}

async function resultFixture(page: Page, theme: string, language: string, state = "failed") {
  const fixture = await parityFixture(page, { theme, language });
  await assistantFixture(page);
  await page.route("**/api/proxy/agent/session/transcript**", route => route.fulfill({ json: {
    ok: true, session_id: "parity-session", title: "Review BTC strategy evidence", count: 2,
    messages: [
      { message_id: "request", role: "user", content: "Review the saved BTC strategy and its one-year replay evidence. Do not activate trading.", ts: sourceTime },
      { message_id: "result", role: "assistant", content: report, ts: sourceTime, turn: {
        turn_id: "review-turn", stopped_reason: "end_turn", reply_text: report,
        artifact_index: state === "missing" ? {} : { ...artifactIndex, tests_run: state === "failed" ? artifactIndex.tests_run : [] },
        verifier_outcome: { hard_passed: state === "passed", hard_status: state, trusted: state === "passed",
          details: { hard: { checks: state === "missing" ? [] : [
            { check_id: "contract", label: "Strategy package contract", status: "passed", required: true,
              artifact_id: "strategy:btc_review:proposal:fixture", revision: "a".repeat(64) },
            { check_id: "coverage", label: "Requested historical coverage", status: state, required: true,
              message: "Historical coverage: 302 of 365 requested days. Isolated fixture.",
              artifact_id: "strategy:btc_review:proposal:fixture", revision: "a".repeat(64) },
          ], unverified: ["out_of_sample", "live_execution"] } },
        },
        blocks: [{ block: { kind: "text", text: report } }],
      } },
    ],
  } }));
  await page.goto("/chat/parity-session");
  const open = page.locator('[data-turn-role="assistant"] button').filter({ hasText: /Open in tab|在标签|标签页/ }).first();
  await expect(open).toBeVisible();
  await open.click();
  await expect(page.getByTestId("canvas-result-body")).toContainText("Strategy replay review");
  return fixture;
}

const skillRows = [
  ["strategy_author", "Create, edit and validate strategies. 策略编写、修改与验证。", 1],
  ["backtest", "Historical replay, data coverage and out-of-sample checks. 历史回测、数据覆盖与样本外验证。", 0],
  ["research", "Collect source-backed market research and reports. 投研、来源核对与研究报告。", 0],
  ["markets", "Quotes, candles and market-data routing. 行情与数据源。", 0],
  ["browser", "Observe, act and verify in the work browser. 浏览器操作与验证。", 0],
  ["tasks", "Manage scheduled work without changing trading permissions. 定时任务。", 0],
].map(([id, description, method_count]) => ({
  id: String(id), description: String(description), method_count: Number(method_count),
  source: "builtin", revision: "fixture-revision", entry: "SKILL.md", enabled: true,
  assigned: true, edit_effect: "workspace_override", catalog_group: "core",
}));

async function skillsFixture(page: Page, theme: string, language: string) {
  const fixture = await parityFixture(page, { theme, language });
  await assistantFixture(page);
  const writes: unknown[] = [];
  await page.route("**/api/proxy/mcp-settings/roles", route => route.fulfill({ json: { roles: [{ name: "researcher" }] } }));
  await page.route("**/api/proxy/skills/catalog**", route => {
    const url = new URL(route.request().url());
    const query = (url.searchParams.get("query") || "").toLowerCase();
    const rows = skillRows.filter(row => `${row.id} ${row.description}`.toLowerCase().includes(query));
    return route.fulfill({ json: { skills: rows, total: rows.length, next_offset: null, enabled_revision: "1", binding_revision: "1" } });
  });
  await page.route("**/api/proxy/skills/read**", async route => {
    const id = new URL(route.request().url()).searchParams.get("skill_id") || "research";
    if (!skillRows.some(row => row.id === id)) return route.fulfill({ status: 404, json: { error: "fixture_skill_not_found" } });
    const text = await readFile(resolve(__dirname, `../../../nerya/skills/builtin/${id}/SKILL.md`), "utf8");
    return route.fulfill({ json: { id, source: "builtin", file: "SKILL.md", revision: "fixture-revision", text, total_chars: text.length, next_offset: null, files: ["SKILL.md"] } });
  });
  await page.route("**/api/proxy/skills/manage", route => {
    writes.push(route.request().postDataJSON());
    return route.fulfill({ json: { ok: true, applied: true } });
  });
  await page.goto("/skills");
  await expect(page.getByTestId("skill-catalog").locator("[data-skill-id]")).toHaveCount(skillRows.length);
  return { ...fixture, writes };
}

for (const view of [
  { width: 1440, theme: "dark", language: "en" },
  { width: 1440, theme: "light", language: "zh" },
  { width: 390, theme: "light", language: "zh" },
]) {
  const name = `${view.width}-${view.theme}-${view.language}`;
  test(`evidence review ${name}`, async ({ page }, info) => {
    await page.setViewportSize({ width: view.width, height: 960 });
    const fixture = await resultFixture(page, view.theme, view.language);
    await capture(page, info, `results-${name}`);
    await page.getByTestId("result-evidence-tab").click();
    await expect(page.getByTestId("canvas-result-body")).toBeHidden();
    await expect(page.getByTestId("delivery-evidence")).toContainText("302 of 365");
    await expect(page.getByTestId("delivery-evidence").locator("details")).toHaveAttribute("open", "");
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await capture(page, info, `evidence-${name}`);
    expect(fixture.errors).toEqual([]);
    expect(fixture.requests).toEqual([]);
  });
  test(`skills library ${name}`, async ({ page }, info) => {
    await page.setViewportSize({ width: view.width, height: 960 });
    const fixture = await skillsFixture(page, view.theme, view.language);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await capture(page, info, `skills-${name}`);
    const search = page.locator('input[placeholder]').first();
    await search.fill("样本外");
    await expect(page.getByTestId("skill-catalog").locator("[data-skill-id]")).toHaveCount(1);
    await expect(page.locator('[data-skill-id="backtest"]')).toBeVisible();
    await page.locator('[data-skill-id="backtest"]').getByRole("button").first().click();
    await expect(page.getByRole("dialog")).toContainText("SKILL.md");
    await capture(page, info, `skill-detail-${name}`);
    expect(fixture.writes).toEqual([]);
    expect(fixture.errors).toEqual([]);
  });
}

for (const state of ["missing", "pending", "stale", "passed"]) {
  test(`evidence state ${state} remains distinct after reload`, async ({ page }) => {
    const fixture = await resultFixture(page, "light", "en", state);
    await expect(page.getByTestId("delivery-evidence")).toHaveAttribute("data-state", state);
    await page.getByTestId("result-evidence-tab").focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("result-evidence-tab")).toHaveAttribute("aria-pressed", "true");
    await page.reload();
    const open = page.locator('[data-turn-role="assistant"] button').filter({ hasText: /Open in tab/ }).first();
    await open.click();
    await expect(page.getByTestId("delivery-evidence")).toHaveAttribute("data-state", state);
    expect(fixture.requests).toEqual([]);
    expect(fixture.errors).toEqual([]);
  });
}

test("skill search resets cleanly and preview never changes assignment", async ({ page }) => {
  const fixture = await skillsFixture(page, "light", "en");
  const search = page.getByRole("textbox", { name: "Search Skills" });
  await search.fill("no_matching_skill_fixture");
  await expect(page.getByTestId("skill-catalog").locator("[data-skill-id]")).toHaveCount(0);
  await page.getByRole("button", { name: "Reset filters", exact: true }).click();
  const open = page.getByRole("button", { name: "Read: backtest", exact: true });
  await open.focus(); await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(fixture.writes).toEqual([]);
  expect(fixture.errors).toEqual([]);
});
