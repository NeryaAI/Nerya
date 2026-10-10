import assert from "node:assert/strict";
import { installFixture, order } from "./model-priority.ego.mjs";

function fixture() {
  const initialProfile = (provider, kind) => ({ provider, base_url: `https://${provider}.invalid/v1`, kind, provider_key_ref: `vault://${provider}-saved`, has_key_ref: true, declared: { base_url: `https://${provider}.invalid/v1`, kind, provider_key_ref: `vault://${provider}-saved` } });
  const persisted = JSON.parse(sessionStorage.getItem("provider-management-fixture") || "null");
  const state = window.__providerManagementQA = { profiles: persisted?.profiles || [initialProfile("openai", "chat_completions"), initialProfile("anthropic", "anthropic_messages")], models: persisted?.models || { openai: ["fixture-A", "fixture-C"], anthropic: ["fixture-B"] }, writes: [], failRefresh: false, failSave: false };
  if (!state.profiles.some(profile => profile.provider === "custom-desk")) state.profiles.push(initialProfile("custom-desk", "chat_completions"));
  const base = window.__modelPriorityQA;
  const original = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const path = url.pathname.replace(/^\/api\/proxy/, "");
    const body = init?.body ? JSON.parse(init.body) : {};
    const config = () => ({ ...base.config, provider_profiles: state.profiles, revision: "provider-fixture-revision" });
    function persist() { sessionStorage.setItem("provider-management-fixture", JSON.stringify({ profiles: state.profiles, models: state.models })); }
    if (path === "/llm/config") {
      if (init?.method === "POST") {
        state.writes.push({ path, body });
        if (state.failSave) return Response.json({ ok: false, error: "simulated failure" }, { status: 503 });
        for (const patch of body.providers || []) {
          const current = state.profiles.find(profile => profile.provider === patch.provider);
          for (const key of ["base_url", "kind", "provider_key_ref"]) if (key in patch) {
            current[key] = patch[key]; current.declared[key] = patch[key];
          }
          if (patch.provider_key) { current.provider_key_ref = "vault://replacement-saved"; current.declared.provider_key_ref = current.provider_key_ref; }
        }
        persist();
      }
      return Response.json(config());
    }
    if (path === "/llm/models") return Response.json({ providers: Object.fromEntries(Object.entries(state.models).map(([provider, ids]) => [provider, ids.map(id => ({ id }))])) });
    if (path === "/llm/models/provider-refresh") {
      state.writes.push({ path, body });
      if (state.failRefresh) return Response.json({ ok: false, error: "simulated refresh failure" }, { status: 503 });
      state.models[body.provider] = ["refreshed-one", "refreshed-two", "refreshed-three"];
      persist();
      return Response.json({ ok: true, provider: body.provider, count: 3, models: state.models[body.provider].map(id => ({ id })) });
    }
    return original(input, init);
  };
}

export async function installProviderFixture(page) {
  await installFixture(page);
  await page.cdp("Page.addScriptToEvaluateOnNewDocument", { source: `(${fixture.toString()})()` });
}

export async function verifyProviderManagement(page, baseUrl) {
  await installProviderFixture(page);
  await page.goto(`${baseUrl}/settings#models`);
  await page.waitForSelector('[data-testid="provider-management"]');
  const alpha = '[data-testid="provider-management"] li[data-provider-id="openai"]';
  const editor = '[data-testid="provider-editor"]';
  const save = `${editor} button:text-is("Save connection")`;
  const edit = `${alpha} button[aria-label="Edit connection for openai"]`;
  const refresh = `${alpha} button[aria-label="Update models for openai"]`;
  const text = () => page.evaluate(selector => document.querySelector(selector).textContent, alpha);
  await page.click(refresh);
  await page.waitForFunction(() => document.querySelector('[data-provider-id="openai"]').textContent.includes("Updated 3 models"));
  assert.deepEqual(await page.evaluate(() => window.__providerManagementQA.writes[0].body), { provider: "openai" });
  assert.deepEqual(await page.evaluate(() => window.__providerManagementQA.models.anthropic), ["fixture-B"]);

  await page.press('[data-model-priority-group="primary-models"][data-model-priority-index="0"] button[aria-label^="Drag"]', "ArrowDown");
  const routeDraft = await order(page);
  await page.click(edit);
  assert.equal(await page.evaluate(() => document.querySelector('[aria-label="Replace API key"]').value), "");
  assert.equal(await page.evaluate(() => document.querySelector('[aria-label="Replace API key"]').type), "password");
  await page.fill(`${editor} input[aria-label="API endpoint"]`, "https://edited.invalid/v1");
  await page.fill(`${editor} input[aria-label="Replace API key"]`, "fixture-new-key");
  await page.click(`${editor} button[aria-label="API format"]`);
  await page.click('loc=role:menuitemradio[name="Anthropic Messages"]');
  await page.click(save);
  await page.waitForFunction(() => !document.querySelector('[data-testid="provider-editor"]'));
  const saved = await page.evaluate(() => window.__providerManagementQA.writes.at(-1).body);
  assert.deepEqual(Object.keys(saved), ["providers"]);
  assert.deepEqual(saved.providers, [{ provider: "openai", base_url: "https://edited.invalid/v1", kind: "anthropic_messages", provider_key: "fixture-new-key" }]);
  assert.deepEqual(await order(page), routeDraft);
  assert.equal(await page.evaluate(() => document.querySelector('[data-testid="saved-model-test"] button').disabled), true);
  assert(!(await text()).includes("fixture-new-key"));
  assert((await text()).includes("https://edited.invalid/v1"));

  await page.click(edit);
  await page.fill(`${editor} input[aria-label="Replace API key"]`, "discarded-fixture-key");
  const writesBeforeCancel = await page.evaluate(() => window.__providerManagementQA.writes.length);
  await page.click(`${editor} button:text-is("Cancel")`);
  assert.equal(await page.evaluate(() => window.__providerManagementQA.writes.length), writesBeforeCancel);
  await page.click(edit);
  assert.equal(await page.evaluate(() => document.querySelector('[aria-label="Replace API key"]').value), "");
  await page.fill(`${editor} input[aria-label="API endpoint"]`, "invalid endpoint");
  await page.click(save);
  assert((await text()).includes("HTTP or HTTPS"));
  assert.equal(await page.evaluate(() => window.__providerManagementQA.writes.length), writesBeforeCancel);

  await page.fill(`${editor} input[aria-label="API endpoint"]`, "https://retry.invalid/v1");
  await page.evaluate(() => { window.__providerManagementQA.failSave = true; });
  await page.click(save);
  await page.waitForFunction(() => document.querySelector('[data-provider-id="openai"]').textContent.includes("Save failed"));
  assert.equal(await page.evaluate(() => document.querySelector('[aria-label="API endpoint"]').value), "https://retry.invalid/v1");
  await page.evaluate(() => { window.__providerManagementQA.failSave = false; });
  await page.click(save);
  await page.waitForFunction(() => !document.querySelector('[data-testid="provider-editor"]'));
  const patch = await page.evaluate(() => window.__providerManagementQA.writes.at(-1).body.providers[0]);
  assert.deepEqual(patch, { provider: "openai", base_url: "https://retry.invalid/v1" });
  await page.evaluate(() => { window.__providerManagementQA.failRefresh = true; });
  await page.click(refresh);
  await page.waitForFunction(() => document.querySelector('[data-provider-id="openai"]').textContent.includes("Update failed"));
  assert.deepEqual(await page.evaluate(() => window.__providerManagementQA.models.openai), ["refreshed-one", "refreshed-two", "refreshed-three"]);
  assert.deepEqual(await order(page), routeDraft);
  assert.deepEqual(await page.evaluate(() => window.__modelPriorityQA.errors), []);
}

export async function verifyCustomEndpointRequired(page) {
  const row = '[data-provider-id="custom-desk"]';
  await page.click(`${row} button[aria-label="Edit connection for custom-desk"]`);
  const before = await page.evaluate(() => window.__providerManagementQA.writes.length);
  await page.fill('[data-testid="provider-editor"] input[aria-label="API endpoint"]', "");
  await page.click('[data-testid="provider-editor"] button:text-is("Save connection")');
  assert((await page.evaluate(selector => document.querySelector(selector).textContent, row)).includes("custom provider needs"));
  assert.equal(await page.evaluate(() => window.__providerManagementQA.writes.length), before);
  await page.click('[data-testid="provider-editor"] button:text-is("Cancel")');
  assert.equal(await page.evaluate(selector => document.querySelector(selector).querySelector('[role="alert"]'), row), null);
}
