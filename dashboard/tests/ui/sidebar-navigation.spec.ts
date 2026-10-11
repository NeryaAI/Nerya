import { expect, test, type Page } from "@playwright/test";

async function prepareSidebar(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem("nerya.ui_settings.v1", JSON.stringify({ language: "en", darkMode: "dark" }));
  });

  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api\/proxy/, "");
    let body: unknown = { ok: true, data: {}, items: [], events: [], approvals: [], count: 0 };

    if (path === "/auth/status") {
      body = { ok: true, authenticated: true, password_set: true, enabled: true, local_access: true };
    } else if (path === "/operator/nav") {
      body = {
        ok: true,
        data: {
          primary: [],
          advanced: [
            { id: "browsers", label: "Browsers", href: "/browsers", icon: "browsers", always_visible: true },
          ],
          hidden: [],
          capabilities: {},
        },
      };
    } else if (path === "/agent/sessions") {
      body = { sessions: [], has_more: false };
    } else if (path === "/teams/roles") {
      body = { ok: true, roles: [] };
    } else if (path.startsWith("/skills")) {
      body = { ok: true, skills: [] };
    } else if (path === "/llm/config") {
      body = { ok: true, tiers: [], provider_profiles: [], default_tier: "medium" };
    } else if (path === "/llm/models") {
      body = { providers: {} };
    } else if (path === "/llm/tiers") {
      body = { tiers: [] };
    } else if (path.includes("strategy/list")) {
      body = { ok: true, strategies: [] };
    }

    await route.fulfill({ json: body });
  });

  await page.goto("/agents");
}

test("desktop rail keeps low-frequency destinations inside More", async ({ page }) => {
  await prepareSidebar(page);

  const more = page.getByRole("button", { name: "More", exact: true });
  await expect(more).toBeVisible();
  await expect(more).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByRole("link", { name: "Factor library", exact: true })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Automation", exact: true })).toHaveCount(0);

  await more.click();
  await expect(page.getByRole("menuitem", { name: "Factor library", exact: true })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Automation", exact: true })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Browsers", exact: true })).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(more).toBeFocused();
});

test("mobile drawer exposes the same More menu", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await prepareSidebar(page);

  await page.getByRole("button", { name: "Open navigation", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "Navigation", exact: true });
  await expect(drawer).toBeVisible();

  const more = drawer.getByRole("button", { name: "More", exact: true });
  await more.click();
  await expect(page.getByRole("menuitem", { name: "Factor library", exact: true })).toBeVisible();
  await expect(page.getByRole("menuitem", { name: "Automation", exact: true })).toBeVisible();
});
