import { test, expect, type Page } from "@playwright/test";

const tiers = ["light", "medium", "high", "intent"].map((tier) => ({
  tier, provider: "openai", model: "fixture-model", key_ref: "fixture-only",
}));
const config = { ok: true, default_tier: "medium", intent_tier: "light", tiers, provider_profiles: [], reasoning_levels: ["none", "low", "medium", "high"] };
const notebook = { entries: [], used_chars: 0, char_limit: 4000, usage_pct: 0 };

async function mockDashboard(page: Page, options: { failModelsOnce?: boolean } = {}) {
  const requests: string[] = [];
  const errors: string[] = [];
  let failModels = Boolean(options.failModelsOnce);
  let savedConfig = structuredClone(config);
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(() => {
    if (!localStorage.getItem("nerya.ui_settings.v1")) {
      localStorage.setItem("nerya.ui_settings.v1", JSON.stringify({ language: "en", darkMode: "dark" }));
    }
  });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api\/proxy/, "");
    requests.push(`${route.request().method()} ${path}`);
    let body: unknown = { ok: true, items: [], count: 0, total: 0 };
    if (path === "/llm/config") {
      if (failModels) {
        failModels = false;
        await route.fulfill({ status: 503, json: { ok: false, error: "Fixture: model service unavailable" } });
        return;
      }
      if (route.request().method() === "POST") {
        const update = route.request().postDataJSON() as { default_tier?: string };
        savedConfig = { ...savedConfig, default_tier: update.default_tier ?? savedConfig.default_tier };
      }
      body = savedConfig;
    } else if (path === "/llm/providers") {
      body = { providers: [{ provider: "openai", adapter_present: true, ready: true, configured_tiers: ["light", "medium", "high"], has_key_ref: true }], count: 1 };
    } else if (path === "/llm/models") {
      body = { providers: { openai: [{ id: "fixture-model" }] } };
    } else if (path === "/llm/catalog") {
      body = { providers: [], reasoning_levels: ["none", "low", "medium", "high"] };
    } else if (path === "/llm/oauth/providers") {
      body = { providers: [], statuses: {} };
    } else if (path === "/llm/tiers") {
      body = { tiers, count: tiers.length };
    } else if (path === "/market/venues") {
      body = { venues: [{ name: "binance", label: "Binance", public: true }] };
    } else if (path === "/operator/nav") {
      body = { ok: true, data: { primary: [], advanced: [] }, primary: [], advanced: [] };
    } else if (path === "/operator/overview") {
      body = { status: "ok", data: { attention: [], counts: {}, accounts: [], strategies: [] } };
    } else if (path === "/setup/readiness") {
      body = { status: "ok", summary: "Fixture ready", data: { checks: [], blocking: [] } };
    } else if (path === "/workspace") {
      body = { root: "fixture", live_trading_enabled: false, kill_switch: false };
    } else if (path === "/health") {
      body = { status: "ok" };
    } else if (path === "/portfolio/summary") {
      body = { accounts: [], totals: { cash_usd: 0, equity_usd: 0 } };
    } else if (path === "/portfolio/pnl") {
      body = { equity_usd: 0, realized_usd: 0, total_pnl_usd: 0 };
    } else if (path === "/accounts/list") {
      body = { accounts: [], ts: 0 };
    } else if (path === "/agent/sessions") {
      body = { sessions: [], has_more: false };
    } else if (path === "/agent/session") {
      body = { error: "fixture session not persisted" };
    } else if (path === "/agent/stream/events") {
      body = { events: [], latest_seq: 0, cursor: 0, count: 0 };
    } else if (path === "/agent/run_turn_internal") {
      body = { status: "ok", turn_id: "fixture-turn", decision: { action: "respond", text: "UI fixture reply" }, actions: [], artifacts: [] };
    } else if (path === "/market/candles") {
      body = { candles: [] };
    } else if (path === "/agent/attachments/upload") {
      const input = route.request().postDataJSON() as { attachments: Record<string, unknown>[] };
      body = { ok: true, attachments: input.attachments.map(({ data_url: _data, ...file }) => ({ ...file, uploaded: true, artifact_uri: `artifact://fixture/${file.id}` })) };
    } else if (path.includes("strategy/list")) {
      body = { ok: true, strategies: [] };
    } else if (path === "/memory/vector/status") {
      body = { ok: true, enabled: false, dependency_available: false, paths: ["memory"], watch_enabled: false };
    } else if (path === "/memory/external/config") {
      body = { enabled: true, provider: "agentmemory", agentmemory: { base_url: "http://fixture.invalid", secret_ref: "fixture-only", timeout_seconds: 30 } };
    } else if (path === "/memory/notebook") {
      body = { targets: ["agent", "operator"], agent: { ...notebook, target: "agent" }, operator: { ...notebook, target: "operator" } };
    } else if (path === "/memory/activity") {
      body = { events: [], stats: { write_ok: 0, write_skipped: 0, search: 0 } };
    } else if (path === "/memory/write_rules") {
      body = { categories: [], dedupe_strategies: [], rules: {}, warnings: [] };
    } else if (path === "/memory/providers") {
      body = { builtin: null, external: null, available_external: [] };
    } else if (path === "/auth/status") {
      body = { ok: true, authenticated: true, password_set: true, enabled: true };
    } else if (path === "/data/financial_datasets/status") {
      body = { ok: true, available: false, configured: false, keys: [] };
    } else if (path === "/browsers/status") {
      body = { ok: true, browsers: [], engines: [], active: null };
    } else if (path === "/search/engines/status") {
      body = { ok: true, engines: [], chain: [], safe_search: true };
    }
    // Never forward even an unexpected mutation to the real runtime.
    await route.fulfill({ status: 200, json: body });
  });
  return { requests, errors };
}

async function interfacePage(page: Page) {
  await page.goto("/settings#interface");
  await expect(page.getByRole("button", { name: "Dark", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeEnabled();
}
async function changeTier(page: Page) {
  await expect(page.getByRole("button", { name: "Save assignments", exact: true })).toBeVisible();
  const control = page.locator("label").filter({ hasText: "Default tier" }).getByRole("button");
  await control.click();
  await page.getByRole("menuitemradio").last().click();
  await expect(page.getByRole("button", { name: "Save assignments", exact: true })).toBeEnabled();
}

for (const width of [320, 390]) {
  test(`mobile ${width}px: full-width settings, drawer and focus recovery`, async ({ page }, info) => {
    const state = await mockDashboard(page);
    await page.setViewportSize({ width, height: 844 });
    await interfacePage(page);
    await expect(page.locator(".ui-desktop-navigation")).toBeHidden();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.locator("#settings-panel-interface").evaluate((element) => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    const open = page.getByRole("button", { name: "Open navigation", exact: true });
    await open.click();
    await expect(page.getByRole("dialog", { name: "Navigation" })).toBeVisible();
    await page.screenshot({ path: info.outputPath(`navigation-${width}.png`) });
    await page.getByRole("button", { name: "Interface", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await expect(open).toBeFocused();
    await page.screenshot({ path: info.outputPath(`settings-${width}.png`), fullPage: true });
    expect(state.errors).toEqual([]);
  });
}

test("interface loads no models, memory, secrets or network settings", async ({ page }, info) => {
  const state = await mockDashboard(page);
  await interfacePage(page);
  expect(state.requests.filter((request) => /\/(llm|memory|secrets|network)\//.test(request))).toEqual([]);
  expect(state.requests.filter((request) => request === "GET /market/venues")).toHaveLength(1);
  await page.screenshot({ path: info.outputPath("settings-desktop.png"), fullPage: true });
  expect(state.errors).toEqual([]);
});

test("settings search gives an empty state and can be cleared", async ({ page }) => {
  await mockDashboard(page);
  await interfacePage(page);
  await page.getByRole("textbox", { name: "Search settings…" }).fill("no-such-setting-xyz");
  await expect(page.getByRole("status", { name: "" }).filter({ hasText: "No matching settings" })).toBeVisible();
  await page.getByRole("button", { name: "Clear search" }).click();
  await expect(page.getByRole("button", { name: "Interface", exact: true })).toHaveAttribute("aria-current", "page");
});

test("Radix choice menu supports keyboard selection and returns focus", async ({ page }, info) => {
  const state = await mockDashboard(page);
  await interfacePage(page);
  await page.getByRole("button", { name: "Dark", exact: true }).focus();
  await page.keyboard.press("ArrowDown");
  const light = page.getByRole("menuitemradio", { name: "Light", exact: true });
  await expect(light).toBeVisible();
  await light.focus();
  await page.keyboard.press("Enter");
  const selectedTheme = page.getByRole("button", { name: "Light", exact: true });
  await expect(selectedTheme).toBeFocused();
  await expect(selectedTheme).toHaveCSS("background-color", "rgb(240, 236, 255)");
  await expect(selectedTheme).toHaveCSS("color", "rgb(26, 24, 48)");
  await expect(page.getByRole("button", { name: "Interface", exact: true })).toHaveCSS("color", "rgb(26, 24, 48)");
  await page.screenshot({ path: info.outputPath("settings-light.png"), fullPage: true, animations: "disabled" });
  expect(state.errors).toEqual([]);
});

test("language change updates document language without remounting settings", async ({ page }) => {
  const state = await mockDashboard(page);
  await interfacePage(page);
  await page.getByRole("button", { name: "English", exact: true }).click();
  await page.getByRole("menuitemradio", { name: /中文/ }).click();
  await expect(page.locator("html")).toHaveAttribute("lang", "zh");
  expect(state.requests.filter((request) => request === "GET /market/venues")).toHaveLength(1);
  expect(state.errors).toEqual([]);
});

test("search hover then Enter chooses the hovered destination", async ({ page }) => {
  const state = await mockDashboard(page);
  await interfacePage(page);
  await page.keyboard.press("Control+k");
  await expect(page.getByRole("combobox")).toBeFocused();
  await page.getByRole("option", { name: /Memory/ }).hover();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/memory$/);
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test("search Escape restores focus; IME Enter does not select a destination", async ({ page }) => {
  await mockDashboard(page);
  await interfacePage(page);
  const theme = page.getByRole("button", { name: "Dark", exact: true });
  await theme.focus();
  await page.keyboard.press("Control+k");
  const input = page.getByRole("combobox");
  await expect(input).toBeFocused();
  await input.dispatchEvent("keydown", { key: "Enter", code: "Enter", isComposing: true });
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(theme).toBeFocused();
});

test("model refresh focuses Cancel; Enter cancels and keeps the draft", async ({ page }) => {
  const state = await mockDashboard(page);
  await page.goto("/settings#models");
  const advanced = page.getByRole("button", { name: "Advanced model routing", exact: true });
  await expect(advanced).toHaveAttribute("aria-expanded", "false");
  await changeTier(page);
  const count = state.requests.filter((request) => request === "GET /llm/config").length;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Save assignments", exact: true })).toBeEnabled();
  expect(state.requests.filter((request) => request === "GET /llm/config")).toHaveLength(count);
  expect(state.errors).toEqual([]);
});

test("confirmed model refresh reloads saved config exactly once", async ({ page }) => {
  const state = await mockDashboard(page);
  await page.goto("/settings#models");
  await changeTier(page);
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Confirm action", exact: true }).click();
  await expect.poll(() => state.requests.filter((request) => request === "GET /llm/config").length).toBe(2);
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Save assignments", exact: true })).toBeDisabled();
  expect(state.errors).toEqual([]);
});

test("model service failure stays visible and retry restores the form", async ({ page }) => {
  const state = await mockDashboard(page, { failModelsOnce: true });
  await page.goto("/settings#models");
  await expect(page.getByRole("alert").filter({ hasText: "Settings could not be loaded" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save assignments", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByRole("button", { name: "Save assignments", exact: true })).toBeVisible();
  expect(state.requests.filter((request) => request === "GET /llm/config")).toHaveLength(2);
  expect(state.errors).toEqual([]);
});

test("switching setting panels preserves model edits without refetching", async ({ page }) => {
  const state = await mockDashboard(page);
  await page.goto("/settings#models");
  await changeTier(page);
  await page.getByRole("button", { name: "Interface", exact: true }).click();
  await expect(page.getByRole("button", { name: "Dark", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Models", exact: true }).click();
  await expect(page.getByRole("button", { name: "Save assignments", exact: true })).toBeEnabled();
  expect(state.requests.filter((request) => request === "GET /llm/config")).toHaveLength(1);
  expect(state.errors).toEqual([]);
});

test("memory activity deep link avoids unrelated notebook and model requests", async ({ page }) => {
  const state = await mockDashboard(page);
  await page.goto("/memory?tab=activity");
  await expect(page.locator("header").getByRole("button", { name: "Refresh", exact: true })).toBeEnabled();
  await expect.poll(() => state.requests.includes("POST /memory/activity")).toBe(true);
  expect(state.requests.filter((request) => request.includes("/llm/") || request.includes("/memory/notebook"))).toEqual([]);
  await expect(page.getByRole("radio", { name: /AgentMemory/i })).toHaveAttribute("aria-checked", "true");
  expect(state.errors).toEqual([]);
});

const file = (name: string) => ({ name, mimeType: "text/plain", buffer: Buffer.from("UI regression attachment") });

for (const route of ["/", "/chat"]) {
  for (const width of [320, 390, 1440]) {
    test(`composer ${route} at ${width}px fits and preserves IME input`, async ({ page }, info) => {
      const state = await mockDashboard(page);
      await page.setViewportSize({ width, height: 900 });
      await page.goto(route);
      const input = page.locator("[data-chat-composer] textarea");
      await expect(input).toBeVisible();
      await input.fill("测试尚未发送的内容");
      await input.dispatchEvent("keydown", { key: "Enter", code: "Enter", isComposing: true });
      await expect(input).toHaveValue("测试尚未发送的内容");
      expect(state.requests.some((request) => request.includes("/agent/run_turn"))).toBe(false);
      expect(await page.locator("[data-chat-composer]").evaluate((element) => {
        const rect = element.getBoundingClientRect();
        return rect.left >= 0 && rect.right <= innerWidth + 1 && element.scrollWidth <= element.clientWidth + 1;
      })).toBe(true);
      await page.screenshot({ path: info.outputPath("composer.png"), fullPage: true });
      expect(state.errors).toEqual([]);
    });
  }
}

test("home attachment button opens a picker without losing the draft", async ({ page }) => {
  const state = await mockDashboard(page);
  await page.goto("/");
  await page.locator("textarea").fill("Keep this draft");
  const picker = page.waitForEvent("filechooser");
  await page.getByRole("button", { name: "Attach file", exact: true }).click();
  await (await picker).setFiles(file("note.txt"));
  await expect(page.getByText("note.txt", { exact: true })).toBeVisible();
  await expect(page.locator("textarea")).toHaveValue("Keep this draft");
  await expect(page).toHaveURL(/\/$/);
  expect(state.errors).toEqual([]);
});

test("server rejects an upload with HTTP 200: no false-success attachment", async ({ page }) => {
  const state = await mockDashboard(page);
  await page.route("**/api/proxy/agent/attachments/upload", (route) => route.fulfill({ status: 200, json: { ok: true, attachments: [{ id: "bad", name: "bad.txt", uploaded: false, reason: "persist_failed" }] } }));
  await page.goto("/");
  await page.locator("textarea").fill("Keep the message");
  await page.locator('input[type="file"]').setInputFiles(file("bad.txt"));
  await expect(page.locator("[data-chat-composer]").getByRole("alert")).toBeVisible();
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
  await expect(page.getByText("bad.txt", { exact: true })).toHaveCount(0);
  await expect(page.locator("textarea")).toHaveValue("Keep the message");
  expect(state.errors).toEqual([]);
});

test("too many files are rejected before reading or uploading", async ({ page }) => {
  const state = await mockDashboard(page);
  await page.goto("/");
  await page.locator('input[type="file"]').setInputFiles(Array.from({ length: 9 }, (_, index) => file(`${index}.txt`)));
  await expect(page.getByRole("alert").filter({ hasText: "Up to 8 files" })).toBeVisible();
  expect(state.requests.filter((request) => request.includes("attachments/upload"))).toEqual([]);
});

test("upload blocks send and cannot restore an attachment removed while waiting", async ({ page }) => {
  const state = await mockDashboard(page);
  await page.goto("/");
  await page.locator("textarea").fill("Analyze these");
  await page.locator('input[type="file"]').setInputFiles(file("old.txt"));
  await expect(page.getByText("old.txt", { exact: true })).toBeVisible();
  let release!: () => void;
  let started = false;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/proxy/agent/attachments/upload", async (route) => {
    started = true;
    await gate;
    const input = route.request().postDataJSON() as { attachments: Record<string, unknown>[] };
    await route.fulfill({ json: { ok: true, attachments: input.attachments.map(({ data_url: _data, ...file }) => ({ ...file, uploaded: true, artifact_uri: "artifact://fixture/new" })) } });
  });
  try {
    await page.locator('input[type="file"]').setInputFiles(file("new.txt"));
    await expect.poll(() => started).toBe(true);
    await expect(page.getByRole("button", { name: "Send", exact: true })).toBeDisabled();
    await page.locator("textarea").press("Enter");
    expect(state.requests.some((request) => request.includes("/agent/run_turn"))).toBe(false);
    await page.getByRole("button", { name: "Remove attachment: old.txt", exact: true }).click();
    release();
    await expect(page.getByText("new.txt", { exact: true })).toBeVisible();
    await expect(page.getByText("old.txt", { exact: true })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
    expect(state.errors).toEqual([]);
  } finally { release(); }
});

test("home sends text and uploaded reference exactly once across chat navigation", async ({ page }) => {
  const state = await mockDashboard(page);
  const turns: { payload: { text: string; attachments: Record<string, unknown>[] } }[] = [];
  page.on("request", (request) => { if (request.url().includes("/agent/run_turn_internal")) turns.push(request.postDataJSON()); });
  await page.goto("/");
  await page.locator("textarea").fill("Review the attachment");
  await page.locator('input[type="file"]').setInputFiles(file("report.txt"));
  await expect(page.getByText("report.txt", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => turns.length).toBe(1);
  await expect(page).toHaveURL(/\/chat\/.+/);
  expect(turns[0].payload.text).toBe("Review the attachment");
  expect(turns[0].payload.attachments[0].artifact_uri).toMatch(/^artifact:\/\/fixture\//);
  expect(turns[0].payload.attachments[0].data_url).toBeUndefined();
  await expect(page.getByRole("button", { name: "Send", exact: true })).toBeVisible();
  expect(turns).toHaveLength(1);
  expect(state.errors).toEqual([]);
});

test("model options remain inside a 320px viewport, including nested settings", async ({ page }, info) => {
  const state = await mockDashboard(page);
  await page.setViewportSize({ width: 320, height: 900 });
  await page.goto("/");
  const trigger = page.getByRole("button", { name: "Model options", exact: true });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "Model options", exact: true });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Edit: Runtime default", exact: true }).click();
  await dialog.getByRole("button", { name: "Context", exact: true }).click();
  await page.getByRole("menuitemradio", { name: "128k", exact: true }).click();
  await expect(dialog).toBeVisible();
  expect(await dialog.evaluate((element) => { const rect = element.getBoundingClientRect(); return rect.left >= 0 && rect.right <= innerWidth && element.scrollWidth <= element.clientWidth; })).toBe(true);
  await page.screenshot({ path: info.outputPath("model-options-mobile.png"), fullPage: true });
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  expect(state.errors).toEqual([]);
});

test("model selection displays actual model name, never a fabricated tier version", async ({ page }) => {
  const state = await mockDashboard(page);
  await page.goto("/");
  const trigger = page.getByRole("button", { name: "Model options", exact: true });
  await trigger.click();
  await page.getByRole("dialog", { name: "Model options" }).getByRole("button", { name: /^fixture-model openai/ }).first().click();
  await expect(trigger).toContainText("fixture-model");
  await expect(trigger).not.toContainText(/5\.[345]/);
  expect(state.errors).toEqual([]);
});

test("interface symbol commits on confirmation and never saves an empty value", async ({ page }) => {
  await mockDashboard(page);
  await interfacePage(page);
  const symbol = page.getByRole("textbox", { name: "Symbol", exact: true });
  await symbol.fill("ethusdt");
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("nerya.ui_settings.v1") || "{}").kline?.symbol)).not.toBe("ethusdt");
  await symbol.press("Enter");
  await expect(symbol).toHaveValue("ETHUSDT");
  await symbol.fill("");
  await symbol.press("Tab");
  await expect(symbol).toHaveValue("ETHUSDT");
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("nerya.ui_settings.v1") || "{}").kline.symbol)).toBe("ETHUSDT");
});

test("cancel resetting interface preferences preserves chart settings", async ({ page }) => {
  await mockDashboard(page);
  await interfacePage(page);
  await page.getByRole("textbox", { name: "Symbol", exact: true }).fill("ETHUSDT");
  await page.getByRole("textbox", { name: "Symbol", exact: true }).press("Enter");
  await page.getByRole("button", { name: "Reset", exact: true }).click();
  await expect(page.getByRole("button", { name: "Cancel", exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("textbox", { name: "Symbol", exact: true })).toHaveValue("ETHUSDT");
});

test("successful model save is followed by fresh saved configuration, not cache", async ({ page }) => {
  const state = await mockDashboard(page);
  await page.goto("/settings#models");
  await changeTier(page);
  const choice = await page.locator("label").filter({ hasText: "Default tier" }).getByRole("button").textContent();
  await page.getByRole("button", { name: "Save assignments", exact: true }).click();
  await expect(page.getByRole("button", { name: "Save assignments", exact: true })).toBeDisabled();
  await page.getByRole("link", { name: "Memory", exact: true }).click();
  await expect(page).toHaveURL(/\/memory$/);
  await page.getByRole("button", { name: "Models", exact: true }).click();
  await expect.poll(() => state.requests.filter((request) => request === "GET /llm/config").length).toBe(2);
  await expect(page.getByRole("button", { name: "Save assignments", exact: true })).toBeDisabled();
  await expect(page.locator("label").filter({ hasText: "Default tier" }).getByRole("button")).toHaveText(choice || "");
});

test("dashboard health error clears after a successful retry", async ({ page }, info) => {
  const state = await mockDashboard(page);
  let failed = false;
  await page.route("**/api/proxy/health", async (route) => {
    if (!failed) { failed = true; await route.fulfill({ status: 503, json: { error: "fixture-health-offline" } }); }
    else await route.fulfill({ json: { status: "ok" } });
  });
  await page.goto("/dashboard");
  const error = page.getByRole("alert").filter({ hasText: "fixture-health-offline" });
  await expect(error).toBeVisible();
  await error.getByRole("button", { name: "Try again" }).click();
  await expect(error).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("dashboard.png"), fullPage: true });
  expect(state.errors).toEqual([]);
});

test("canceling a slow upload keeps the draft and ignores its eventual result", async ({ page }) => {
  const state = await mockDashboard(page);
  let release!: () => void;
  let started = false;
  let finished = false;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/proxy/agent/attachments/upload", async (route) => {
    started = true;
    await gate;
    // Aborting fetch can dispose this request before the mock server responds.
    await route.fulfill({ json: { ok: true, attachments: [{ id: "cancelled", name: "cancelled.txt", artifact_uri: "artifact://fixture/cancelled", uploaded: true }] } }).catch(() => {});
    finished = true;
  });
  try {
    await page.goto("/");
    await page.locator("textarea").fill("Keep this unsent draft");
    await page.locator('input[type="file"]').setInputFiles(file("cancelled.txt"));
    await expect.poll(() => started).toBe(true);
    await page.locator("[data-chat-composer]").getByRole("button", { name: "Cancel", exact: true }).click();
    await expect(page.getByRole("button", { name: "Send", exact: true })).toBeEnabled();
    release();
    await expect.poll(() => finished).toBe(true);
    await expect(page.getByText("cancelled.txt", { exact: true })).toHaveCount(0);
    await expect(page.locator("textarea")).toHaveValue("Keep this unsent draft");
    expect(state.errors).toEqual([]);
  } finally { release(); }
});

test("failed session storage writes cannot replace a fresh home draft with an old one", async ({ page }) => {
  const state = await mockDashboard(page);
  const texts: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/agent/run_turn_internal")) texts.push(request.postDataJSON().payload.text);
  });
  await page.goto("/");
  await page.locator("textarea").fill("The new draft must win");
  await page.evaluate(() => {
    sessionStorage.setItem("nerya.compose.draft.v2", JSON.stringify({ text: "stale draft", autoSend: true, attachments: [] }));
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (this === sessionStorage) throw new DOMException("Fixture quota exceeded", "QuotaExceededError");
      return original.call(this, key, value);
    };
  });
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect.poll(() => texts.length).toBe(1);
  expect(texts).toEqual(["The new draft must win"]);
  expect(state.errors).toEqual([]);
});

test("late candles from the previous symbol cannot overwrite the current market", async ({ page }) => {
  const state = await mockDashboard(page);
  await page.addInitScript(() => localStorage.setItem("dashboard-market-open", "1"));
  let release!: () => void;
  let oldStarted = false;
  let oldFinished = 0;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/proxy/market/candles", async (route) => {
    const market = route.request().postDataJSON().market;
    if (market === "ETHUSDT") {
      await route.fulfill({ json: { candles: [], error: "current-market-fixture" } });
    } else {
      oldStarted = true;
      await gate;
      await route.fulfill({ json: { candles: [], error: "stale-market-fixture" } });
      oldFinished += 1;
    }
  });
  try {
    await page.goto("/dashboard");
    await expect.poll(() => oldStarted).toBe(true);
    const symbol = page.getByPlaceholder("BTCUSDT", { exact: true });
    await symbol.fill("ETHUSDT");
    await symbol.press("Enter");
    await expect(page.getByText("Failed to load candles: current-market-fixture", { exact: true })).toBeVisible();
    release();
    await expect.poll(() => oldFinished).toBeGreaterThan(0);
    await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(page.getByText("Failed to load candles: current-market-fixture", { exact: true })).toBeVisible();
    await expect(page.getByText("Failed to load candles: stale-market-fixture", { exact: true })).toHaveCount(0);
    expect(state.errors).toEqual([]);
  } finally { release(); }
});

