import assert from "node:assert/strict";
import { installFixture } from "./model-priority.ego.mjs";

function fixture() {
  const original = window.fetch.bind(window);
  const state = window.__savedModelProbeQA = { mode: "success", calls: [] };
  window.fetch = async (input, init) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    if (url.pathname !== "/api/proxy/llm/messages/probe") return original(input, init);
    const body = JSON.parse(init.body);
    state.calls.push(body);
    if (state.mode === "rate_limit") return Response.json({ ok: false, revision: body.revision, failure_kind: "rate_limit", status_code: 429, request_id: "fixture-request" }, { status: 503 });
    if (state.mode === "stale") return Response.json({ ok: false, error: "llm_config_changed", revision: "changed" }, { status: 409 });
    return Response.json({ ok: true, revision: body.revision, provider: "fixture", model: "fixture-A", text_preview: "E2E_READY" });
  };
}

export async function verifySavedProbe(page, url) {
  await installFixture(page);
  await page.cdp("Page.addScriptToEvaluateOnNewDocument", { source: `(${fixture.toString()})()` });
  const target = `${url}/settings#models`;
  if (await page.url() === target) await page.reload();
  else await page.goto(target);
  await page.waitForSelector("[data-testid=saved-model-test]");
  assert.equal(await page.evaluate(() => window.__savedModelProbeQA.calls.length), 0);
  const test = '[data-testid="saved-model-test"] button';
  const text = () => page.evaluate(() => document.querySelector('[data-testid="saved-model-test"]').textContent);
  await page.click(test);
  await page.waitForFunction(() => document.querySelector('[data-testid="saved-model-test"]').textContent.includes("Test passed"));
  assert((await text()).includes("fixture-A"));
  await page.evaluate(() => { window.__savedModelProbeQA.mode = "rate_limit"; });
  await page.click(test);
  await page.waitForFunction(() => document.querySelector('[data-testid="saved-model-test"]').textContent.includes("HTTP 429"));
  assert((await text()).includes("rate limited"));
  assert((await text()).includes("fixture-request"));
  assert.equal(await page.evaluate(() => document.querySelector('[data-testid="saved-model-test"] button').disabled), false);
  await page.evaluate(() => { window.__savedModelProbeQA.mode = "success"; });
  await page.click(test);
  await page.waitForFunction(() => document.querySelector('[data-testid="saved-model-test"]').textContent.includes("Test passed"));
  await page.evaluate(() => { window.__savedModelProbeQA.mode = "stale"; });
  await page.click(test);
  await page.waitForFunction(() => document.querySelector('[data-testid="saved-model-test"]').textContent.includes("Configuration changed"));
  await page.press('[data-model-priority-group="primary-models"][data-model-priority-index="0"] button[aria-label^="Drag"]', "ArrowDown");
  assert.equal(await page.evaluate(() => document.querySelector('[data-testid="saved-model-test"] button').disabled), true);
  assert.equal(await page.evaluate(() => window.__savedModelProbeQA.calls.length), 4);
  assert.deepEqual(await page.evaluate(() => window.__modelPriorityQA.errors), []);
}
