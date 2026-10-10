import assert from "node:assert/strict";

function fixture() {
  const route = (provider, model, effort, window) => ({ provider, model, models: [model], reasoning_effort: effort, context_window: window, base_url: `https://${provider}.invalid/v1`, provider_key_ref: `vault://${model}`, declared: { base_url: `https://${provider}.invalid/v1`, provider_key_ref: `vault://${model}`, reasoning_effort: effort, context_window: window } });
  const initial = { ok: true, default_tier: "medium", intent_tier: "light", revision: "fixture", provider_profiles: [], reasoning_levels: ["none", "low", "high"], tiers: [
    { tier: "medium", routes: [route("openai", "fixture-A", "low", 32000), route("anthropic", "fixture-B", "high", 64000), route("openai", "fixture-C", "none", 128000)] },
    { tier: "light", routes: [route("openai", "fixture-light", "low", 32000), route("openai", "fixture-light-backup", "none", 32000)] },
  ] };
  const state = window.__modelPriorityQA = { config: JSON.parse(sessionStorage.getItem("model-priority-qa") || "null") || initial, writes: [], errors: [], failSave: false };
  localStorage.setItem("nerya.ui_settings.v1", JSON.stringify({ language: "en", darkMode: "dark", refreshSeconds: 0 }));
  window.addEventListener("error", event => state.errors.push(event.message));
  window.addEventListener("unhandledrejection", event => state.errors.push(String(event.reason)));
  const original = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith("/api/")) return original(input, init);
    const path = url.pathname.replace(/^\/api\/proxy/, "");
    let result = { ok: true, items: [], events: [], accounts: [], providers: [], statuses: {} };
    if (path === "/llm/config") {
      if (init?.method === "POST") {
        const body = JSON.parse(init.body);
        state.writes.push(body);
        if (state.failSave) return Response.json({ ok: false, error: "Fixture save rejected" }, { status: 503 });
        state.config = { ...state.config, ...body, revision: "saved-fixture" };
        sessionStorage.setItem("model-priority-qa", JSON.stringify(state.config));
      }
      result = state.config;
    } else if (path === "/auth/status") result = { ok: true, authenticated: true, local_access: true, password_set: true, enabled: true };
    else if (path === "/llm/models") result = { providers: { openai: ["fixture-A", "fixture-C", "fixture-light", "fixture-light-backup"].map(id => ({ id })), anthropic: [{ id: "fixture-B" }] } };
    else if (path === "/llm/tiers") result = { tiers: state.config.tiers };
    else if (path === "/llm/providers") result = { providers: ["openai", "anthropic"].map(name => ({ provider: name, ready: true, key_present: true })) };
    else if (path === "/operator/nav") result = { ok: true, data: { primary: [], advanced: [] } };
    else if (path === "/setup/readiness") result = { ok: true, data: { checks: [], blocking: [] } };
    else if (path === "/workspace") result = { root: "isolated-model-priority-fixture", live_trading_enabled: false };
    return Response.json(result);
  };
}

export async function installFixture(page) {
  return page.cdp("Page.addScriptToEvaluateOnNewDocument", { source: `(${fixture.toString()})()` });
}

export async function order(page, group = "primary-models") {
  return page.evaluate(group => [...document.querySelectorAll("[data-model-priority-group]")]
    .filter(row => row.dataset.modelPriorityGroup === group)
    .map(row => row.querySelector("[role=combobox]")?.textContent.trim()), group);
}

export async function verifySave(page, expected) {
  await page.click('loc=role:button[name="Save assignments"]');
  await page.waitForFunction(() => window.__modelPriorityQA.writes.length > 0);
  const payload = await page.evaluate(() => window.__modelPriorityQA.writes.at(-1));
  const medium = payload.tiers.find(row => row.tier === "medium");
  assert.deepEqual(medium.routes.map(row => row.models[0]), expected);
  for (const row of medium.routes) {
    assert.equal(row.provider_key_ref, `vault://${row.models[0]}`);
    assert.equal(row.reasoning_effort, { "fixture-A": "low", "fixture-B": "high", "fixture-C": "none" }[row.models[0]]);
  }
  assert.deepEqual(payload.tiers.find(row => row.tier === "light").routes.map(row => row.model), ["fixture-light", "fixture-light-backup"]);
}
