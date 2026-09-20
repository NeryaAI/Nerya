import { test, expect, type Page } from "@playwright/test";
import en from "../../messages/en.json";
import zh from "../../messages/zh.json";

// Isolated UI acceptance. These are example roles, never a connected runtime.
async function fixture(page: Page, locale: "en" | "zh" = "en", theme = "dark") {
  const roles = [
    { name: "market_analyst", tier: "medium", source: "default", allowed_skills: ["research", "markets"], prompt: "# Market analyst\n\nReview market conditions using verified sources. Identify missing inputs before continuing." },
    { name: "risk_reviewer", tier: "high", source: "workspace", allowed_skills: ["research"], prompt: "# Risk reviewer\n\nCheck assumptions and missing evidence before reviewing a decision." },
    { name: "quant_researcher", tier: "high", source: "default", allowed_skills: ["research"], prompt: "# Quant researcher\n\nEvaluate the supplied data and document the limits of a backtest." },
  ];
  const state = { roles, failDetail: "", failSave: false, failList: false, errors: [] as string[], writes: [] as { path: string; body: Record<string, unknown> }[] };
  page.on("pageerror", (error) => state.errors.push(error.message));
  page.on("console", (message) => { if (message.type() === "error") state.errors.push(message.text()); });
  await page.addInitScript(({ locale, theme }) => {
    localStorage.setItem("nerya.ui_settings.v1", JSON.stringify({ language: locale, darkMode: theme }));
  }, { locale, theme });
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api\/proxy/, "");
    const input = route.request().postDataJSON() || {};
    let body: unknown = { ok: true, data: {}, items: [], events: [], approvals: [], count: 0 };
    if (path === "/auth/status") body = { ok: true, authenticated: true, password_set: true, enabled: true };
    else if (path === "/teams/roles") body = state.failList ? { ok: false, error: "List unavailable" } : { ok: true, roles };
    else if (path === "/teams/role/get") {
      const role = roles.find((item) => item.name === input.name);
      body = state.failDetail === input.name || !role ? { ok: false, error: "This role is unavailable" } : { ok: true, role: { ...role, persistent: true } };
    } else if (path === "/teams/role/save") {
      state.writes.push({ path, body: input });
      const role = { ...input, source: "workspace" } as typeof roles[number];
      if (state.failSave) body = { ok: false, error: "Could not save this role" };
      else {
        const index = roles.findIndex((item) => item.name === role.name);
        if (index < 0) roles.push(role); else roles[index] = role;
        body = { ok: true, role: { ...role, persistent: true } };
      }
    } else if (path === "/teams/role/delete") {
      state.writes.push({ path, body: input });
      const index = roles.findIndex((item) => item.name === input.name);
      if (index >= 0) roles.splice(index, 1);
      body = { ok: true, deleted: true, name: input.name };
    } else if (path.startsWith("/skills")) body = { ok: true, skills: [{ id: "research", title: "Research" }, { id: "markets", title: "Market data" }] };
    else if (path === "/operator/nav") body = { ok: true, data: { primary: [], advanced: [] }, primary: [], advanced: [] };
    else if (path === "/operator/overview") body = { status: "ok", data: { attention: [], counts: {}, accounts: [], strategies: [] } };
    else if (path === "/agent/sessions") body = { sessions: [], has_more: false };
    else if (path === "/accounts/list") body = { accounts: [], ts: 0 };
    else if (path === "/llm/config") body = { ok: true, tiers: [], provider_profiles: [], default_tier: "medium" };
    else if (path === "/llm/models") body = { providers: {} };
    else if (path === "/llm/tiers") body = { tiers: [] };
    else if (path.includes("strategy/list")) body = { ok: true, strategies: [] };
    await route.fulfill({ json: body });
  });
  await page.goto("/agents?agent=market_analyst");
  await expect(page.getByTestId("agent-overview")).toBeVisible();
  return state;
}

const views = [
  { width: 1440, theme: "dark", locale: "zh" as const },
  { width: 1440, theme: "light", locale: "en" as const },
  { width: 390, theme: "light", locale: "zh" as const },
  { width: 320, theme: "dark", locale: "en" as const },
];

for (const view of views) {
  test(`agent overview and filters ${view.width}px ${view.theme} ${view.locale}`, async ({ page }, info) => {
    await page.setViewportSize({ width: view.width, height: 960 });
    const state = await fixture(page, view.locale, view.theme);
    const t = (view.locale === "zh" ? zh : en).agentsPage;
    const details = page.getByTestId("agent-details");
    await expect(details.getByRole("heading", { name: t.responsibility })).toBeVisible();
    await expect(details).toContainText("Review market conditions using verified sources.");
    await expect(details.locator("textarea")).toHaveCount(0);
    await expect(details.getByRole("button", { name: t.editConfiguration })).toBeVisible();
    await page.screenshot({ path: info.outputPath(`agents-${view.width}-${view.theme}-${view.locale}.png`) });
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    const list = page.getByRole("list", { name: t.title, exact: true });
    await page.getByRole("button", { name: t.filterCustom, exact: true }).click();
    await expect(list.getByRole("button")).toHaveCount(1);
    await expect(list).toContainText(t.roleNames.risk_reviewer);
    await page.getByRole("textbox", { name: t.searchPlaceholder }).fill("no-such-role");
    await expect(page.getByText(t.noMatchingAgents, { exact: true })).toBeVisible();
    await page.getByRole("button", { name: t.clearFilters }).click();
    await expect(list.getByRole("button")).toHaveCount(3);
    await page.getByRole("textbox", { name: t.searchPlaceholder }).fill(t.roleNames.market_analyst);
    await expect(list.getByRole("button")).toHaveCount(1);
    await expect(details).toContainText("market_analyst");
    expect(state.writes).toEqual([]); expect(state.errors).toEqual([]);
  });
}

test("role selection failure removes stale content and can be retried", async ({ page }) => {
  const state = await fixture(page);
  state.failDetail = "risk_reviewer";
  await page.getByRole("list", { name: "Agents", exact: true }).getByRole("button", { name: /Risk reviewer/ }).click();
  const details = page.getByTestId("agent-details");
  await expect(details).toContainText("This role is unavailable");
  await expect(details).not.toContainText("Review market conditions");
  await expect(details.getByRole("button", { name: "Edit configuration" })).toHaveCount(0);
  state.failDetail = "";
  await details.getByRole("button", { name: "Retry" }).click();
  await expect(details).toContainText("Check assumptions and missing evidence");
  await expect(page).toHaveURL(/agent=risk_reviewer/);
  expect(state.errors).toEqual([]);
});

test("editing preserves drafts across cancelled switches and save failures", async ({ page }) => {
  const state = await fixture(page);
  await page.getByRole("button", { name: "Edit configuration", exact: true }).click();
  const editor = page.getByTestId("agent-editor"), input = editor.locator("textarea");
  const draft = "Preserve this research brief and require source verification.";
  await input.fill(draft);
  await expect(page.getByRole("button", { name: "New agent", exact: true })).toBeDisabled();
  await page.getByRole("list", { name: "Agents", exact: true }).getByRole("button", { name: /Risk reviewer/ }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(input).toHaveValue(draft);
  await expect(page).toHaveURL(/agent=market_analyst/);
  state.failSave = true;
  await editor.getByRole("button", { name: "Save custom version" }).click();
  await expect(page.getByText("Could not save this role", { exact: true })).toBeVisible();
  await expect(input).toHaveValue(draft);
  state.failSave = false;
  await editor.getByRole("button", { name: "Save custom version" }).click();
  await expect(page.getByTestId("agent-overview")).toContainText(draft);
  expect(state.writes).toHaveLength(2);
  expect(state.writes[1].body).toMatchObject({ name: "market_analyst", prompt: draft, allowed_skills: ["research", "markets"] });
  expect(state.errors).toEqual([]);
});

test("create dialog traps and restores focus, retains draft, validates names and creates once", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const state = await fixture(page, "en", "light");
  const trigger = page.getByRole("button", { name: "New agent", exact: true });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "Create agent" });
  const name = dialog.getByRole("textbox", { name: /^Name/ });
  await expect(name).toBeFocused();
  await name.fill("bad name");
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("Use only letters");
  await name.fill("market_analyst");
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("already in use");
  expect(state.writes).toEqual([]);
  await name.fill("evidence_reviewer");
  await dialog.getByRole("button", { name: "Add", exact: true }).click();
  const research = dialog.getByRole("checkbox", { name: "Research", exact: true });
  await research.click();
  await expect(research).toBeChecked();
  await expect(dialog.getByRole("button", { name: "Remove Research", exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Add", exact: true }).click();
  await page.screenshot({ path: info.outputPath("create-agent-mobile-light.png") });
  for (let index = 0; index < 14; index++) {
    await page.keyboard.press("Tab");
    expect(await dialog.evaluate((node) => node.contains(document.activeElement))).toBe(true);
  }
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible(); await expect(trigger).toBeFocused();
  await trigger.click(); await expect(name).toHaveValue("evidence_reviewer");
  await dialog.getByRole("button", { name: "Create", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page).toHaveURL(/agent=evidence_reviewer/);
  await expect(page.getByTestId("agent-overview")).toBeVisible();
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0].body.allowed_skills).toEqual(["research"]);
  expect(state.writes[0].body.prompt).toContain("# evidence_reviewer");
  expect(state.writes[0].body.prompt).not.toContain("<role-name>");
  expect(state.errors).toEqual([]);
});

test("light and dark primary buttons and body text have readable contrast", async ({ page }) => {
  await fixture(page);
  for (const theme of ["dark", "light"]) {
    await page.evaluate((value) => document.documentElement.classList.toggle("light", value === "light"), theme);
    const contrast = await page.evaluate(() => {
      const rgb = (value: string) => (value.match(/[\d.]+/g) || []).slice(0, 3).map(Number);
      const luminance = (channels: number[]) => channels.map((channel) => { const v = channel / 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; }).reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
      const ratio = (a: number[], b: number[]) => { const x = luminance(a), y = luminance(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); };
      const button = getComputedStyle(document.querySelector(".btn-primary")!);
      const sample = document.createElement("span"); document.body.appendChild(sample);
      const root = getComputedStyle(document.documentElement);
      sample.style.color = root.getPropertyValue("--text-muted"); sample.style.backgroundColor = root.getPropertyValue("--card");
      const body = getComputedStyle(sample), bodyRatio = ratio(rgb(body.color), rgb(body.backgroundColor)); sample.remove();
      return { button: ratio(rgb(button.color), rgb(button.backgroundColor)), body: bodyRatio };
    });
    expect(contrast.button).toBeGreaterThanOrEqual(4.5); expect(contrast.body).toBeGreaterThanOrEqual(4.5);
  }
});

test("direct entry to a secondary destination reveals its active navigation", async ({ page }) => {
  const state = await fixture(page);
  await page.goto("/accounts");
  await expect(page.getByRole("link", { name: "Trading", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Trading", exact: true })).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("button", { name: "More", exact: true })).toHaveAttribute("aria-expanded", "true");
  expect(state.errors).toEqual([]);
});
