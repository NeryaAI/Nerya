import { chooseOption } from "./choice-control";
import { test, expect, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";

const session = "agent-workspace-fixture";
const title = "Audit the research evidence";
const report = "## Evidence review completed.\n\nThe research and review passes are complete. The source register is ready for the next step.\n\n### What was checked\n\nSource attribution, publication dates, and consistency between the findings.\n\n### Remaining work\n\nConfirm the two outstanding source links before publishing. These records are UI test fixtures, not production research.";
const trace = [
  { block: { kind: "tool_use", action: "read_file", skill_id: "native", call_id: "lead-read", payload: { path: "notes/sources.md" } } },
  { block: { kind: "tool_result", action: "read_file", skill_id: "native", call_id: "lead-read", ok: true, result: { content: "Source register inspected." } } },
  { block: { kind: "text", text: report } },
  { block: { kind: "text", text: "## Evidence review completed." } },
];
const recordedDiff = "--- a/notes/sources.md\n+++ b/notes/sources.md\n@@ -10,2 +10,3 @@\n Source register\n-Unverified attribution\n+Original source linked\n+Publication date recorded";
const previewHtml = "<!doctype html><html><body><main><h1>Evidence report preview</h1><p>12 sources reviewed. Two dates need confirmation.</p></main></body></html>";
const richTrace = [...trace.slice(0, 2),
  { block: { kind: "tool_use", action: "edit_file", call_id: "edit-evidence", payload: { path: "notes/sources.md" } } },
  { block: { kind: "tool_result", action: "edit_file", call_id: "edit-evidence", ok: true, result: { diff: recordedDiff } } },
  { block: { kind: "tool_use", action: "create_file", call_id: "create-report", payload: { path: "reports/evidence.html" } } },
  { block: { kind: "tool_result", action: "create_file", call_id: "create-report", ok: true, result: { content: previewHtml } } },
  { block: { kind: "tool_use", action: "run_shell", call_id: "verify", payload: { command: "python verify_sources.py" } } },
  { block: { kind: "tool_result", action: "run_shell", call_id: "verify", ok: false, error: "Two dates still need confirmation." } },
  { block: { kind: "tool_use", action: "write_file", call_id: "unfinished", payload: { path: "notes/not-written.md", content: "Not a completed file" } } },
  ...trace.slice(2),
];
const now = Date.now() / 1000;
const context = { parent_session_id: session, scope: "subagent", inherited_messages: 4, saved_messages: 12, allowed_skills: ["web_search", "file_read"], model: "fixture-model" };

async function fixture(page: Page, options: { legacy?: boolean; theme?: string; multiple?: boolean; empty?: boolean; rich?: boolean; title?: string; answer?: string; locale?: "en" | "zh" } = {}) {
  const taskTitle = options.title || title;
  const taskAnswer = options.answer || report;
  const errors: string[] = [];
  const writes: { path: string; body: Record<string, unknown> }[] = [];
  const agents = [
    { id: "agent_research", name: "Researcher", state: "running", activity: { action: "web_search" } },
    { id: "agent_review", name: "Reviewer", state: "completed", activity: { kind: "completed" } },
  ].map((a) => ({ ...a, session_id: session, group_id: "task-one", parent_call_id: "team-call", title: taskTitle, attempt: 1, updated_at: now, context, output: { summary: "Verified the primary sources." } }));
  const messages: Record<string, unknown>[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript(({ theme, locale }) => {
    if (window.top !== window) return;
    localStorage.setItem("nerya.ui_settings.v1", JSON.stringify({ language: locale, darkMode: theme }));
  }, { theme: options.theme || "dark", locale: options.locale || "en" });
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api\/proxy/, "");
    const input = route.request().method() === "POST" ? route.request().postDataJSON() || {} : {};
    let body: unknown = { ok: true, items: [], count: 0, total: 0, events: [], approvals: [] };
    const meta = { session_id: session, title: taskTitle, created_at: new Date(now * 1000).toISOString(), updated_at: new Date(now * 1000).toISOString(), message_count: 2 };
    if (path === "/agent/sessions") body = { sessions: [meta], has_more: false };
    else if (path === "/agent/session") body = { ...meta, id: session, messages: [] };
    else if (path === "/agent/session/transcript") body = { ok: true, ...meta, count: 2, messages: [
      { message_id: "user-one", role: "user", content: taskTitle, ts: meta.created_at },
      ...(!options.empty ? [{ message_id: "answer-one", role: "assistant", content: taskAnswer, ts: meta.updated_at,
        turn: options.legacy ? { subagents: { Researcher: { summary: "Historical findings" } } } : { turn_id: "turn-one", reply_text: taskAnswer, blocks: options.answer ? [...trace.slice(0, 2), { block: { kind: "text", text: taskAnswer } }] : options.rich ? richTrace : trace } }] : []),
      ...(options.multiple ? [
        { message_id: "user-two", role: "user", content: "Review the revised source register", ts: new Date((now + 1) * 1000).toISOString() },
        { message_id: "answer-two", role: "assistant", content: "Second review result.", ts: new Date((now + 2) * 1000).toISOString(), turn: { turn_id: "turn-two", final_text: "Second review result." } },
      ] : []),
    ] };
    else if (path === "/teams/agents") body = { ok: true, agents: options.legacy || options.empty || url.searchParams.get("session_id") !== session ? [] : agents };
    else if (path === "/teams/agents/get") {
      const agent = agents.find((a) => a.id === input.agent_id)!;
      body = { ok: true, agent, has_more: false, messages: messages.filter((m) => m.recipient === agent.id), events: [
        { seq: 1, kind: "started", ts: now - 10, data: { attempt: agent.attempt } },
        { seq: 2, kind: "tool_use", ts: now - 8, data: { action: "web_search", call_id: "call-1", payload: { query: "primary evidence sources" }, attempt: agent.attempt } },
        { seq: 3, kind: "tool_result", ts: now - 6, data: { action: "web_search", call_id: "call-1", ok: true, result: { sources: ["Verified primary source"] }, attempt: agent.attempt } },
      ] };
    } else if (path === "/teams/agents/message") {
      writes.push({ path, body: input });
      const message = { id: input.request_id, sender: "operator", recipient: input.agent_id, content: input.message, status: "queued", ts: now };
      messages.push(message); body = { ok: true, message };
    } else if (path === "/teams/agents/resume") {
      writes.push({ path, body: input });
      const agent = agents.find((a) => a.id === input.agent_id)!;
      agent.attempt += 1; agent.updated_at += 1;
      body = { ok: true, agent_id: agent.id, state: "completed" };
    } else if (path === "/agent/stream/events") body = { events: [], latest_seq: 0, cursor: 0, count: 0 };
    else if (path === "/auth/status") body = { ok: true, authenticated: true, password_set: true, enabled: true };
    else if (path === "/operator/nav") body = { ok: true, data: { primary: [], advanced: [] }, primary: [], advanced: [] };
    else if (path === "/operator/overview") body = { status: "ok", data: { attention: [], counts: {}, accounts: [], strategies: [] } };
    else if (path === "/workspace") body = { root: "fixture", live_trading_enabled: false, kill_switch: false };
    else if (path === "/health") body = { status: "ok" };
    else if (path === "/llm/config") body = { ok: true, tiers: [], provider_profiles: [], default_tier: "medium", reasoning_levels: ["none", "low", "medium", "high"] };
    else if (path === "/llm/providers" || path === "/llm/catalog") body = { providers: [] };
    else if (path === "/llm/tiers") body = { tiers: [], count: 0 };
    else if (path === "/market/venues") body = { venues: [] };
    else if (path === "/accounts/list") body = { accounts: [], ts: 0 };
    else if (path.includes("strategy/list")) body = { ok: true, strategies: [] };
    // Never forward unexpected requests to the real runtime.
    await route.fulfill({ status: 200, json: body });
  });
  await page.goto(`/chat/${session}`);
  await expect(page.getByRole("tablist", { name: options.locale === "zh" ? "任务工作区" : "Task workspace", exact: true })).toBeVisible();
  if (!options.empty) await expect(page.getByTestId("agent-task-bar")).toBeVisible();
  return { errors, writes, agents, messages };
}

for (const theme of ["dark", "light"]) {
  test(`main agent uses the same readable operation UX and result handoff (${theme})`, async ({ page }, info) => {
    const { errors } = await fixture(page, { rich: true, theme });
    const assistant = page.locator('#chat-workspace-panel-conversation [data-turn-role="assistant"]').first();
    const summary = assistant.locator('[data-turn-section="trace"] > details > summary');
    await expect(summary).toContainText("5 operations");
    await expect(summary).toContainText("1 need review");
    await summary.click();
    const trace = assistant.getByTestId("lead-readable-trace");
    await expect(trace.getByTestId("agent-operation")).toHaveCount(5);
    await expect(trace.getByTestId("agent-debug-json")).toHaveCount(0);
    const read = trace.getByTestId("agent-operation").first();
    await read.locator(":scope > summary").click();
    await expect(read).toContainText("Source register inspected.");
    await expect(trace).toContainText("Two dates still need confirmation.");
    await expect(trace).toContainText("No response recorded");
    await page.screenshot({ path: info.outputPath(`minara-readable-conversation-${theme}.png`) });
    await read.getByTestId("agent-debug").locator("summary").click();
    await expect(read.getByTestId("agent-debug-json")).toContainText("lead-read");
    await read.getByTestId("agent-debug").locator("summary").click();
    await summary.click();
    await expect(trace).not.toBeVisible();
    const actions = assistant.getByTestId("result-actions");
    expect(await actions.locator("div").first().evaluate((node) => getComputedStyle(node).opacity)).toBe("1");
    await actions.getByTestId("open-result-canvas").click();
    await expect(page.getByTestId("canvas-result-body")).toContainText("Evidence review completed.");
    await expect(assistant.getByRole("region", { name: "Final result" })).toBeVisible();
    await page.screenshot({ path: info.outputPath(`minara-result-handoff-${theme}.png`) });
    expect(errors).toEqual([]);
  });
}

const workspace = (page: Page) => page.getByRole("tablist", { name: "Task workspace", exact: true });
const memberInput = (page: Page) => page.getByTestId("agent-work-panel").locator("textarea:visible");
const canvasTabs = (page: Page) => page.getByRole("tablist", { name: "Canvas content", exact: true });

for (const theme of ["dark", "light"]) {
  test(`original theme and sidebar remain intact (${theme})`, async ({ page }) => {
    await fixture(page, { theme });
    const inherited = await page.getByTestId("chat-workbench").evaluate((el) => {
      const root = getComputedStyle(document.documentElement), local = getComputedStyle(el);
      return ["--ink-950", "--ink-100", "--bg", "--card-hi", "--text-muted"].every((key) => root.getPropertyValue(key) === local.getPropertyValue(key))
        && local.fontFamily === getComputedStyle(document.body).fontFamily;
    });
    expect(inherited).toBe(true);
    await expect(page.getByRole("button", { name: "Customize", exact: true })).toHaveCount(0);
  });
}

test("readable child conversation, explicit debug and source-aware Canvas", async ({ page }, info) => {
  const state = await fixture(page);
  const child = state.agents[0];
  const narrative = "I am checking the original references and recording any missing evidence.";
  const output = { summary: "The source review is complete.", findings: ["12 source records were checked.", "Two publication dates still need confirmation."], next_steps: ["Confirm the missing dates before publication."] };
  const events: { seq: number; kind: string; ts: number; data: Record<string, unknown> }[] = [
    { kind: "instruction", data: { text: "Review the source register and flag any gaps." } },
    { kind: "text", data: { text: narrative } },
    { kind: "tool_use", data: { action: "web_search", call_id: "search", payload: { query: "primary evidence sources" } } },
    { kind: "tool_result", data: { action: "web_search", call_id: "search", ok: true, result: { sources: [{ title: "Primary source register", url: "https://example.com/evidence", snippet: "Original references and publication dates." }] } } },
    { kind: "tool_use", data: { action: "read_file", call_id: "file", payload: { path: "notes/sources.md" } } },
    { kind: "tool_result", data: { action: "read_file", call_id: "file", ok: true, result: { content: "Two citations still need their original publication date." } } },
    { kind: "tool_use", data: { action: "edit_file", call_id: "edit", payload: { path: "notes/sources.md" } } },
    { kind: "tool_result", data: { action: "edit_file", call_id: "edit", ok: true, result: { diff: "- Unverified attribution\n+ Original source linked" } } },
    { kind: "tool_use", data: { action: "run_shell", call_id: "shell", payload: { command: "python verify_sources.py" } } },
    { kind: "tool_result", data: { action: "run_shell", call_id: "shell", ok: true, result: { stdout: "12 source records checked", exit_code: 0 } } },
  ].map((e, i) => ({ ...e, seq: i + 1, ts: now - 30 + i, data: { ...e.data, attempt: 1 } }));
  await page.route("**/api/proxy/teams/agents/get", (route) => route.fulfill({ json: { ok: true, agent: child, events, messages: [], has_more: false } }));
  await workspace(page).getByRole("tab", { name: "Agents", exact: true }).click();
  const panel = page.locator("#agent-members-panel-agent_research");
  await expect(panel.getByTestId("agent-conversation")).toBeVisible();
  await expect(panel.getByText(narrative, { exact: true })).toBeVisible();
  await expect(panel.locator('[data-presentation="flat"]')).toHaveCount(1);
  await expect(panel.getByRole("tablist")).toHaveCount(0);
  await expect(panel.getByTestId("agent-operation")).toHaveCount(4);
  await expect(panel.getByTestId("agent-debug-json")).toHaveCount(0);
  const operations = panel.getByTestId("agent-operation");
  await operations.nth(0).locator(":scope > summary").click();
  await expect(panel.getByRole("link", { name: "Primary source register" })).toHaveAttribute("href", "https://example.com/evidence");
  await expect(panel.getByText("Original references and publication dates.", { exact: true })).toBeVisible();
  await operations.nth(1).locator(":scope > summary").click();
  await expect(panel.getByText("Two citations still need their original publication date.", { exact: true })).toBeVisible();
  await operations.nth(2).locator(":scope > summary").click();
  await expect(operations.nth(2).locator("pre")).toContainText("+ Original source linked");
  await operations.nth(3).locator(":scope > summary").click();
  await expect(operations.nth(3).locator("pre")).toContainText("12 source records checked");
  await expect(panel.getByTestId("agent-debug-json")).toHaveCount(0);
  await operations.nth(0).getByTestId("agent-debug").locator("summary").click();
  await expect(panel.getByTestId("agent-debug-json")).toContainText('"call_id"');
  await operations.nth(0).getByTestId("agent-debug").locator("summary").click();
  await expect(panel.getByTestId("agent-debug-json")).toHaveCount(0);
  await panel.getByTestId("agent-detail-content").evaluate((el) => { el.scrollTop = 0; });
  await page.screenshot({ path: info.outputPath("child-readable-process.png") });
  child.state = "completed"; child.updated_at += 1; child.output = output;
  events.push({ seq: 11, kind: "completed", ts: now, data: { attempt: 1, output } });
  await expect(panel.locator('[data-turn-section="reply"]')).toContainText("The source review is complete.");
  await expect(panel.locator('[data-turn-section="reply"]')).toContainText("Confirm the missing dates before publication.");
  await page.screenshot({ path: info.outputPath("child-readable-result.png") });
  await panel.getByTestId("open-result-canvas").click();
  await expect(page.getByTestId("canvas-result-body")).toContainText("The source review is complete.");
  await page.screenshot({ path: info.outputPath("child-canvas-result.png") });
  await page.getByRole("button", { name: "Show agent conversation", exact: true }).click();
  await expect(panel).toBeVisible();
  await expect(panel.locator('[data-presentation="flat"]')).toBeFocused();
  child.attempt = 2; child.state = "running"; child.updated_at += 1;
  events.push({ seq: 12, kind: "instruction", ts: now + 1, data: { attempt: 2, text: "Check the two remaining dates." } });
  await expect(panel.locator('[data-agent-run="2"]')).toContainText("Check the two remaining dates.");
  await expect(panel.locator('[data-agent-run="2"] [data-turn-section="reply"]')).toHaveCount(0);
  await expect(panel.locator('[data-agent-run="1"] [data-turn-section="reply"]')).toContainText("The source review is complete.");
  expect(state.errors).toEqual([]);
});

for (const theme of ["dark", "light"]) {
  test(`flat conversation, agent tabs and final result canvas (${theme})`, async ({ page }, info) => {
    test.setTimeout(90_000);
    const { errors } = await fixture(page, { theme });
    const bar = page.getByTestId("agent-task-bar");
    const conversation = page.locator("#chat-workspace-panel-conversation");
    await expect(workspace(page).getByRole("tab", { name: "Conversation", exact: true })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByTestId("agent-work-panel")).not.toBeVisible();
    const positions = await Promise.all([bar.boundingBox(), page.locator('[data-chat-composer="docked"]').boundingBox()]);
    expect(positions[0]!.y + positions[0]!.height).toBeLessThanOrEqual(positions[1]!.y + 1);
    const assistant = conversation.locator('[data-turn-role="assistant"]');
    await expect(assistant).toHaveAttribute("data-presentation", "flat");
    await expect(assistant.locator(".bubble-ai, .ring-ai, img")).toHaveCount(0);
    await expect(assistant.getByRole("region", { name: "Final result" })).toBeVisible();
    await assistant.locator('[data-turn-section="trace"] > details > summary').click();
    await expect(assistant.locator('[data-turn-section="trace"]')).not.toContainText("Evidence review completed.");
    expect(await assistant.evaluate((el) => el.lastElementChild?.getAttribute("data-turn-section"))).toBe("reply");
    await assistant.locator('[data-turn-section="trace"] > details > summary').click();
    await page.screenshot({ path: info.outputPath(`conversation-${theme}.png`) });

    await bar.getByRole("button").click();
    const panel = page.getByTestId("agent-work-panel");
    await expect(panel).toBeVisible();
    await expect(conversation).not.toBeVisible();
    await expect(page.getByTestId("canvas-workspace")).not.toBeVisible();
    expect((await page.locator("#chat-workspace-panel-agents").boundingBox())!.width)
      .toBe((await page.getByTestId("chat-workbench").boundingBox())!.width);
    const tabs = panel.getByRole("tablist", { name: "Task members", exact: true });
    await expect(tabs.getByRole("tab")).toHaveCount(2);
    await expect(tabs.getByRole("tab", { name: /Researcher/ })).toHaveAttribute("aria-selected", "true");
    await panel.getByTestId("agent-operation").first().locator(":scope > summary").click();
    await expect(panel.getByText("primary evidence sources", { exact: false })).toBeVisible();
    await page.screenshot({ path: info.outputPath(`agents-tabs-${theme}.png`) });
    await expect(panel.getByTestId("agent-debug-json")).toHaveCount(0);
    await panel.getByTestId("agent-context").locator("summary").click();
    await expect(panel.getByText("Saved conversation messages", { exact: true })).toBeVisible();
    await panel.getByTestId("agent-context").locator("summary").click();
    await panel.getByRole("tab", { name: /Reviewer Completed/ }).click();
    await expect(panel.locator('[data-turn-section="reply"]:visible')).toContainText("Verified the primary sources.");
    await page.screenshot({ path: info.outputPath(`agent-result-${theme}.png`) });

    await workspace(page).getByRole("tab", { name: "Conversation", exact: true }).click();
    await assistant.getByTestId("open-result-canvas").click();
    await expect(conversation).toBeVisible();
    await expect(page.getByRole("separator", { name: "Resize workspace" })).toBeVisible();
    await expect(panel).not.toBeVisible();
    await expect(page.getByTestId("canvas-result-body")).toContainText("Evidence review completed.");
    await page.screenshot({ path: info.outputPath(`result-canvas-${theme}.png`) });
    await page.getByRole("button", { name: "Show in conversation", exact: true }).click();
    await expect(assistant).toBeFocused();
    expect(errors).toEqual([]);
  });
}

test("messages, continuation and drafts keep the existing identity across tab switches", async ({ page }) => {
  const state = await fixture(page);
  const composer = page.locator('[data-chat-composer="docked"] textarea');
  await composer.fill("Unsent lead instruction");
  await page.getByTestId("agent-task-bar").getByRole("button").click();
  const panel = page.getByTestId("agent-work-panel");
  await memberInput(page).fill("Research draft");
  await panel.getByRole("tab", { name: /Reviewer Completed/ }).click();
  await memberInput(page).fill("Review draft");
  await panel.getByRole("tab", { name: /Researcher Running/ }).click();
  await expect(memberInput(page)).toHaveValue("Research draft");
  await workspace(page).getByRole("tab", { name: "Conversation", exact: true }).click();
  await expect(composer).toHaveValue("Unsent lead instruction");
  await workspace(page).getByRole("tab", { name: "Agents", exact: true }).click();
  await memberInput(page).fill("Please check this source.");
  await expect(panel.getByRole("button", { name: "Continue agent", exact: true })).toHaveCount(0);
  await panel.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(panel.getByText("Please check this source.", { exact: true })).toBeVisible();
  expect(state.writes[0].body).toMatchObject({ session_id: session, agent_id: "agent_research", message: "Please check this source." });
  expect(state.writes[0].body.request_id).toBeTruthy();
  await panel.getByRole("tab", { name: /Reviewer Completed/ }).click();
  await expect(memberInput(page)).toHaveValue("Review draft");
  await memberInput(page).fill("Continue the same review.");
  await panel.getByRole("button", { name: "Continue agent", exact: true }).click();
  await expect.poll(() => state.writes.length).toBe(2);
  expect(state.writes[1].body.agent_id).toBe("agent_review");
  await page.reload();
  await page.getByTestId("agent-task-bar").getByRole("button").click();
  await expect(panel.getByText("Please check this source.", { exact: true })).toBeVisible();
  expect(state.agents).toHaveLength(2);
  expect(state.errors).toEqual([]);
});

test("mobile uses one panel with keyboard-operated workspace and member tabs", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { errors } = await fixture(page);
  const first = workspace(page).getByRole("tab", { name: "Conversation", exact: true });
  await first.focus();
  await page.keyboard.press("ArrowRight");
  await expect(workspace(page).getByRole("tab", { name: "Agents", exact: true })).toBeFocused();
  await expect(page.locator("#chat-workspace-panel-conversation")).not.toBeVisible();
  const members = page.getByRole("tablist", { name: "Task members", exact: true });
  await members.getByRole("tab").first().focus();
  await page.keyboard.press("End");
  await expect(members.getByRole("tab").last()).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("agent-work-panel").locator('section[role="tabpanel"]:visible')).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  await page.screenshot({ path: info.outputPath("agent-tabs-mobile.png") });
  await workspace(page).getByRole("tab", { name: "Canvas", exact: true }).click();
  await expect(page.getByTestId("canvas-overview")).toBeVisible();
  await canvasTabs(page).getByRole("tab", { name: "Preview", exact: true }).click();
  await expect(page.getByTestId("canvas-result-body")).toBeVisible();
  await page.screenshot({ path: info.outputPath("result-mobile.png") });
  await workspace(page).getByRole("tab", { name: "Canvas", exact: true }).focus();
  await page.keyboard.press("Home");
  await expect(first).toBeFocused();
  await expect(page.getByTestId("agent-task-bar")).toBeVisible();
  expect(errors).toEqual([]);
});

test("historical children remain inspectable without fabricated resume controls", async ({ page }) => {
  await fixture(page, { legacy: true });
  await page.getByTestId("agent-task-bar").getByRole("button").click();
  const panel = page.getByTestId("agent-work-panel");
  await expect(panel.getByText("Historical findings", { exact: true }).first()).toBeVisible();
  await expect(panel.getByRole("button", { name: "Continue agent", exact: true })).toHaveCount(0);
});

test("each final result opens its own Canvas version and survives refresh", async ({ page }) => {
  await fixture(page, { multiple: true });
  const replies = page.locator('#chat-workspace-panel-conversation [data-turn-section="reply"]');
  await replies.first().getByTestId("open-result-canvas").click();
  await expect(page.getByTestId("canvas-result-body")).toContainText("Evidence review completed.");
  await expect(page.getByTestId("canvas-result-body")).not.toContainText("Second review result.");
  await chooseOption(page.getByRole("combobox", { name: "Preview item", exact: true }), { index: 0 });
  await expect(page.getByTestId("canvas-result-body")).toContainText("Second review result.");
  await page.reload();
  await workspace(page).getByRole("tab", { name: "Canvas", exact: true }).click();
  await canvasTabs(page).getByRole("tab", { name: "Preview", exact: true }).click();
  await expect(page.getByTestId("canvas-result-body")).toContainText("Second review result.");
});

test("switching tabs does not cancel a running lead turn or publish a premature result", async ({ page }) => {
  const state = await fixture(page);
  let release: (() => void) | undefined;
  let started = false;
  await page.route("**/api/proxy/agent/run_turn_internal", async (route) => {
    started = true;
    await new Promise<void>((resolve) => { release = resolve; });
    await route.fulfill({ json: { turn_id: "followup-live", reply_text: "The follow-up is now complete.", actions: [], blocks: [] } });
  });
  await page.locator('[data-chat-composer="docked"] textarea').fill("Review one additional source");
  await page.locator('[data-chat-composer="docked"] textarea').press("Enter");
  await expect.poll(() => started).toBe(true);
  try {
    await expect(page.locator('#chat-workspace-panel-conversation [data-turn-loading="true"]')).toHaveCount(1);
    await expect(page.locator('#chat-workspace-panel-conversation [data-turn-loading="true"] [data-turn-section="reply"]')).toHaveCount(0);
    await workspace(page).getByRole("tab", { name: "Canvas", exact: true }).click();
    await canvasTabs(page).getByRole("tab", { name: "Preview", exact: true }).click();
    await expect(page.getByRole("combobox", { name: "Preview item" }).locator("option").filter({ hasText: /^Main conversation/ })).toHaveCount(1);
    release!();
    await expect(page.getByRole("combobox", { name: "Preview item" }).locator("option").filter({ hasText: /^Main conversation/ })).toHaveCount(2);
    await expect(page.getByTestId("canvas-result-body")).toContainText("The follow-up is now complete.");
    await workspace(page).getByRole("tab", { name: "Conversation", exact: true }).click();
    await expect(page.locator('#chat-workspace-panel-conversation [data-turn-role="assistant"]').last().getByRole("region", { name: "Final result" })).toContainText("The follow-up is now complete.");
    expect(state.errors).toEqual([]);
  } finally { release?.(); }
});

test("empty Agents and Canvas tabs explain missing data without invented results", async ({ page }) => {
  const { errors } = await fixture(page, { empty: true });
  await expect(page.getByTestId("agent-task-bar")).toHaveCount(0);
  await workspace(page).getByRole("tab", { name: "Agents", exact: true }).click();
  await expect(page.getByTestId("agent-work-panel")).toContainText("No collaborators yet");
  await expect(page.getByRole("tablist", { name: "Task members", exact: true })).toHaveCount(0);
  await workspace(page).getByRole("tab", { name: "Canvas", exact: true }).click();
  await expect(page.getByTestId("canvas-overview")).toContainText("Waiting for task output");
  await canvasTabs(page).getByRole("tab", { name: "Preview", exact: true }).click();
  await expect(page.getByTestId("canvas-results-empty")).toBeVisible();
  await expect(page.getByTestId("canvas-result-body")).toHaveCount(0);
  expect(errors).toEqual([]);
});

test("a failed turn stays actionable in conversation and is not added to Canvas results", async ({ page }) => {
  const { errors } = await fixture(page);
  await page.route("**/api/proxy/agent/run_turn_internal", (route) => route.fulfill({ status: 503, json: { error: "fixture_unavailable", detail: "No final report was produced." } }));
  const composer = page.locator('[data-chat-composer="docked"] textarea');
  await composer.fill("Check an unavailable source");
  await composer.press("Enter");
  const failed = page.locator('#chat-workspace-panel-conversation [data-turn-role="assistant"]').last();
  await expect(failed.getByRole("alert")).toBeVisible();
  await expect(failed.locator('[data-turn-section="reply"]')).toHaveCount(0);
  await workspace(page).getByRole("tab", { name: "Canvas", exact: true }).click();
  await expect(page.getByTestId("canvas-overview")).toContainText("Needs attention");
  await canvasTabs(page).getByRole("tab", { name: "Preview", exact: true }).click();
  await page.getByRole("combobox", { name: "Preview item" }).click();
  await expect(page.getByRole("listbox").getByRole("option").filter({ hasText: /^Main conversation/ })).toHaveCount(1);
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("canvas-result-body")).not.toContainText("No final report was produced.");
  expect(errors).toEqual([]);
});

for (const theme of ["dark", "light"]) {
  test(`right workspace supports overview, resize and result reading (${theme})`, async ({ page }, info) => {
    const { errors } = await fixture(page, { theme });
    const source = page.getByTestId("workspace-source");
    const canvas = page.locator("#chat-workspace-panel-canvas");
    const composer = page.locator('[data-chat-composer="docked"] textarea');
    await composer.fill("Keep this draft while I inspect the result.");
    const trigger = page.getByTestId("open-workspace");
    await trigger.click();
    await expect(page.getByTestId("canvas-overview")).toBeVisible();
    await expect(source).toBeVisible();
    await expect(page.getByTestId("agent-work-panel")).not.toBeVisible();
    await expect(page.locator("#canvas-workspace-heading")).toBeFocused();
    const sourceBox = (await source.boundingBox())!, canvasBox = (await canvas.boundingBox())!;
    expect(canvasBox.x).toBeGreaterThanOrEqual(sourceBox.x + sourceBox.width);
    await page.screenshot({ path: info.outputPath(`wegent-overview-${theme}.png`) });
    const separator = page.getByRole("separator", { name: "Resize workspace" });
    await separator.focus(); await page.keyboard.press("Home");
    await expect(separator).toHaveAttribute("aria-valuenow", "38");
    await page.keyboard.press("ArrowLeft");
    await expect(separator).toHaveAttribute("aria-valuenow", "40");
    const handle = (await separator.boundingBox())!;
    await page.mouse.move(handle.x + 2, handle.y + handle.height / 2);
    await page.mouse.down(); await page.mouse.move(handle.x - 45, handle.y + handle.height / 2, { steps: 4 }); await page.mouse.up();
    expect(Number(await separator.getAttribute("aria-valuenow"))).toBeGreaterThan(40);
    await separator.dblclick();
    await expect(separator).toHaveAttribute("aria-valuenow", "48");
    await page.getByTestId("canvas-overview").getByRole("region", { name: "Task results" }).getByRole("button", { name: "Open full preview", exact: true }).click();
    await expect(page.getByTestId("canvas-result-body")).toContainText("Evidence review completed.");
    await page.screenshot({ path: info.outputPath(`wegent-right-result-${theme}.png`) });
    await page.getByRole("group", { name: "Result display" }).getByRole("button", { name: "Markdown", exact: true }).click();
    await expect(page.getByRole("textbox", { name: "Markdown source", exact: true })).toHaveValue(report);
    const downloading = page.waitForEvent("download");
    await page.getByRole("button", { name: "Export Markdown", exact: true }).click();
    const download = await downloading;
    expect(download.suggestedFilename()).toBe(`${title}.md`);
    expect(await readFile((await download.path())!, "utf8")).toBe(report);
    await page.getByRole("button", { name: "Expand workspace", exact: true }).click();
    await expect(source).not.toBeVisible();
    await expect(page.getByRole("textbox", { name: "Markdown source", exact: true })).toHaveValue(report);
    await page.getByRole("button", { name: "Restore side panel", exact: true }).click();
    await expect(source).toBeVisible();
    await page.getByRole("group", { name: "Result display" }).getByRole("button", { name: "Preview", exact: true }).click();
    await page.getByRole("button", { name: "Close workspace", exact: true }).click();
    await expect(canvas).not.toBeVisible(); await expect(trigger).toBeFocused();
    await expect(composer).toHaveValue("Keep this draft while I inspect the result.");
    await trigger.click();
    await expect(page.getByTestId("canvas-result-body")).toContainText("Evidence review completed.");
    await expect(separator).toHaveAttribute("aria-valuenow", "48");
    await page.locator("#canvas-workspace-heading").focus(); await page.keyboard.press("Escape");
    await expect(trigger).toBeFocused();
    expect(errors).toEqual([]);
  });
}

test("overview opens a file preview and a collaborator without parallel agent panels", async ({ page }, info) => {
  const { errors } = await fixture(page);
  await page.getByTestId("open-workspace").click();
  const overview = page.getByTestId("canvas-overview");
  await overview.getByRole("region", { name: "Files and previews" }).getByRole("button", { name: /sources\.md/ }).click();
  await expect(canvasTabs(page).getByRole("tab", { name: "Preview", exact: true })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByRole("combobox", { name: "Preview item" })).toContainText("sources.md");
  await expect(page.getByTestId("workbench-resource-preview")).toContainText("Source register inspected.");
  await page.screenshot({ path: info.outputPath("wegent-file-preview.png") });
  await canvasTabs(page).getByRole("tab", { name: "Overview", exact: true }).click();
  await overview.getByRole("region", { name: "Collaborators" }).getByRole("button", { name: /Reviewer/ }).click();
  await expect(page.getByRole("tablist", { name: "Task members" }).getByRole("tab", { name: /Reviewer/ })).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("canvas-workspace")).not.toBeVisible();
  await expect(page.getByTestId("agent-work-panel").locator('section[role="tabpanel"]:visible')).toHaveCount(1);
  const member = page.locator("#agent-members-panel-agent_review");
  await memberInput(page).fill("Keep this reviewer draft.");
  await member.getByTestId("open-result-canvas").click();
  await expect(member).toBeVisible();
  await expect(page.getByTestId("canvas-result-body")).toContainText("Verified the primary sources.");
  await page.getByRole("button", { name: "Show agent conversation" }).click();
  await expect(member.locator('[data-presentation="flat"]')).toBeFocused();
  await expect(memberInput(page)).toHaveValue("Keep this reviewer draft.");
  expect(errors).toEqual([]);
});

for (const width of [320, 390]) {
  test(`narrow right workspace keeps one surface and returns to the source (${width}px)`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 844 });
    const { errors } = await fixture(page);
    const source = page.getByTestId("workspace-source");
    const trigger = page.locator('#chat-workspace-panel-conversation [data-testid="open-result-canvas"]');
    await page.locator('[data-chat-composer="docked"] textarea').fill("Mobile draft stays here");
    await trigger.click();
    await expect(source).not.toBeVisible();
    await expect(page.getByTestId("canvas-result-body")).toBeVisible();
    await expect(page.getByRole("separator", { name: "Resize workspace" })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    await page.screenshot({ path: info.outputPath(`wegent-mobile-${width}.png`) });
    await page.getByRole("button", { name: "Close workspace" }).click();
    await expect(source).toBeVisible(); await expect(trigger).toBeFocused();
    await expect(page.locator('[data-chat-composer="docked"] textarea')).toHaveValue("Mobile draft stays here");
    expect(errors).toEqual([]);
  });
}

test("reopening a result selects its exact version, not the last manually selected one", async ({ page }) => {
  const { errors } = await fixture(page, { multiple: true });
  const first = page.locator('#chat-workspace-panel-conversation [data-testid="open-result-canvas"]').first();
  await first.click();
  await expect(page.getByTestId("canvas-result-body")).toContainText("Evidence review completed.");
  await page.getByRole("button", { name: "Next item" }).click();
  await expect(page.getByTestId("canvas-result-body")).toContainText("Second review result.");
  await first.click();
  await expect(page.getByTestId("canvas-result-body")).toContainText("Evidence review completed.");
  await page.getByRole("button", { name: "Close workspace" }).click();
  await page.getByTestId("open-workspace").click();
  await expect(page.getByTestId("canvas-result-body")).toContainText("Evidence review completed.");
  await workspace(page).getByRole("tab", { name: "Agents", exact: true }).click();
  await expect(page.getByTestId("canvas-workspace")).not.toBeVisible();
  expect(errors).toEqual([]);
});

// These static srcdoc fixtures contain no service-worker registration. Avoid
// injecting Playwright's worker-block script into an opaque-origin iframe.
// The app sandbox stays strict; all uncaught page errors remain test failures.
test.describe("isolated artifact preview", () => {
  test.use({ serviceWorkers: "allow" });
for (const theme of ["dark", "light"]) {
  test(`whole workbench timeline, file review and independent preview (${theme})`, async ({ page }, info) => {
    const { errors } = await fixture(page, { rich: true, theme });
    await page.getByTestId("open-workspace").click();
    const tabs = canvasTabs(page);
    await expect(tabs.getByRole("tab")).toHaveCount(3);
    await expect(tabs.getByRole("tab")).toHaveText(["Overview", "Files2", "Preview"]);
    await expect(page.getByTestId("workbench-navigation").getByRole("button", { name: "Close workspace" })).toBeVisible();
    const overview = page.getByTestId("canvas-overview");
    const timeline = overview.getByRole("region", { name: "Execution timeline", exact: true });
    await expect(timeline.getByTestId("workbench-timeline-step")).toHaveCount(5);
    await expect(timeline).toContainText("No response recorded");
    await expect(timeline).toContainText("Two dates still need confirmation.");
    await expect(timeline.locator("pre")).toHaveCount(0);
    await timeline.getByRole("button", { name: /Execution timeline/ }).click();
    await expect(page.locator("#workbench-timeline-items")).not.toBeVisible();
    await expect(timeline).toContainText("Read 1");
    await timeline.getByRole("button", { name: /Execution timeline/ }).click();
    await expect(overview.getByTestId("workbench-summary")).toContainText("Evidence review completed.");
    await page.screenshot({ path: info.outputPath(`workbench-overview-${theme}.png`) });
    await tabs.getByRole("tab", { name: /^Files/ }).click();
    const files = page.getByTestId("canvas-file-review");
    await expect(files.getByTestId("workbench-file")).toHaveCount(2);
    await expect(files).not.toContainText("not-written.md");
    await files.getByRole("button", { name: "Expand all", exact: true }).click();
    await expect(files.locator("details[open]")).toHaveCount(2);
    const source = files.getByTestId("workbench-file").filter({ hasText: "notes/sources.md" });
    await expect(source).toContainText("Modified");
    await expect(source.getByTestId("recorded-diff").locator('[data-diff-kind="add"]')).toHaveCount(2);
    await expect(source.getByTestId("recorded-diff").locator('[data-diff-kind="remove"]')).toHaveCount(1);
    await expect(source.getByTestId("recorded-diff").locator('[data-diff-kind="add"]').first()).toContainText("11");
    await page.screenshot({ path: info.outputPath(`workbench-files-${theme}.png`) });
    await chooseOption(source.getByRole("combobox", { name: "File record version" }), { index: 1 });
    await expect(source).toContainText("Source register inspected.");
    await source.getByRole("button", { name: "Open preview", exact: true }).click();
    await expect(tabs.getByRole("tab", { name: "Preview", exact: true })).toBeFocused();
    await expect(page.getByTestId("workbench-resource-preview")).toContainText("Source register inspected.");
    await page.getByRole("button", { name: "Back to files", exact: true }).click();
    await expect(files.locator("details[open]")).toHaveCount(2);
    await files.getByRole("textbox", { name: "Filter files" }).fill("evidence.html");
    await expect(files.getByTestId("workbench-file")).toHaveCount(1);
    await files.getByRole("button", { name: "Open preview", exact: true }).click();
    const frame = page.getByTestId("workbench-resource-preview").locator("iframe");
    await expect(frame).toHaveCount(1);
    await expect(frame).not.toHaveAttribute("sandbox", /allow-same-origin/);
    await expect(frame.contentFrame().getByRole("heading", { name: "Evidence report preview" })).toBeVisible();
    await page.screenshot({ path: info.outputPath(`workbench-preview-${theme}.png`) });
    await page.getByRole("button", { name: "Back to files", exact: true }).click();
    await expect(files.getByRole("textbox", { name: "Filter files" })).toHaveValue("evidence.html");
    await files.getByRole("textbox", { name: "Filter files" }).fill("missing");
    await expect(files).toContainText("No matching files");
    await files.getByRole("button", { name: "Clear filters", exact: true }).click();
    await chooseOption(files.getByRole("combobox", { name: "File scope" }), "changed");
    await expect(files.getByTestId("workbench-file")).toHaveCount(2);
    await files.getByRole("button", { name: "Collapse all", exact: true }).click();
    await expect(files.locator("details[open]")).toHaveCount(0);
    await page.getByRole("button", { name: "Close workspace", exact: true }).click();
    await page.getByTestId("open-workspace").click();
    await expect(files.getByRole("combobox", { name: "File scope" })).toHaveAttribute("data-value", "changed");
    expect(errors).toEqual([]);
  });
}

test("mobile file review preserves navigation and stays within the viewport", async ({ page }, info) => {
  await page.setViewportSize({ width: 320, height: 844 });
  const { errors } = await fixture(page, { rich: true });
  await page.getByTestId("open-workspace").click();
  await expect(page.getByTestId("workspace-source")).not.toBeVisible();
  await canvasTabs(page).getByRole("tab", { name: /^Files/ }).click();
  const files = page.getByTestId("canvas-file-review");
  await files.getByRole("button", { name: "Expand all", exact: true }).click();
  await expect(files.getByTestId("recorded-diff")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  await page.screenshot({ path: info.outputPath("workbench-files-mobile.png") });
  const html = files.getByTestId("workbench-file").filter({ hasText: "reports/evidence.html" });
  await html.getByRole("button", { name: "Open preview", exact: true }).click();
  await expect(page.getByTestId("workbench-resource-preview").locator("iframe")).toBeVisible();
  await page.getByRole("button", { name: "Back to files", exact: true }).click();
  await expect(files.locator("details[open]")).toHaveCount(2);
  await page.getByRole("button", { name: "Close workspace", exact: true }).click();
  await expect(page.getByTestId("workspace-source")).toBeVisible();
  expect(errors).toEqual([]);
});
});

for (const theme of ["dark", "light"]) {
  test(`task topbar is one desktop row with useful task navigation (${theme})`, async ({ page }, info) => {
    const { errors, writes } = await fixture(page, { theme });
    const header = page.getByTestId("task-topbar");
    const titleMenu = header.getByTestId("task-title-menu");
    await expect(header).toHaveCount(1);
    await expect(page.getByTestId("shell-utility-bar")).toHaveCount(0);
    await expect(header.getByRole("link", { name: "Inbox", exact: true })).toHaveCount(1);
    await expect(page.getByRole("link", { name: "Inbox", exact: true })).toHaveCount(1);
    const h = (await header.boundingBox())!, m = (await page.locator("#main-content").boundingBox())!;
    expect(h.y).toBe(m.y);
    expect(h.height).toBeLessThanOrEqual(56);
    const titleBox = (await titleMenu.boundingBox())!, tabsBox = (await workspace(page).boundingBox())!;
    expect(Math.abs(titleBox.y + titleBox.height / 2 - tabsBox.y - tabsBox.height / 2)).toBeLessThan(2);
    await expect(header.getByTestId("task-header-status")).toHaveText("Working");
    await page.getByTestId("open-workspace").click();
    await expect(page.getByTestId("canvas-overview")).toBeVisible();
    await page.screenshot({ path: info.outputPath(`task-topbar-${theme}.png`) });
    await titleMenu.click();
    const menu = page.getByRole("menu", { name: /^Task menu:/ });
    await expect(menu).toContainText(title);
    await page.screenshot({ path: info.outputPath(`task-menu-${theme}.png`) });
    await page.keyboard.press("Escape");
    await expect(titleMenu).toBeFocused();
    await expect(page.getByTestId("canvas-overview")).toBeVisible();
    await titleMenu.click();
    await menu.getByRole("menuitem", { name: /View collaborators/ }).click();
    await expect(workspace(page).getByRole("tab", { name: "Agents", exact: true })).toBeFocused();
    await expect(page.getByTestId("canvas-workspace")).not.toBeVisible();
    await memberInput(page).fill("Preserve member draft from the task menu");
    await titleMenu.click();
    await menu.getByRole("menuitem", { name: "View latest result", exact: true }).click();
    await expect(page.getByTestId("canvas-result-body")).toContainText("Evidence review completed.");
    await page.getByTestId("open-workspace").click();
    await expect(memberInput(page)).toHaveValue("Preserve member draft from the task menu");
    await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
      writeText: async (text: string) => { (window as unknown as { copiedLink: string }).copiedLink = text; },
    } }));
    await titleMenu.click();
    await menu.getByRole("menuitem", { name: "Copy conversation link", exact: true }).click();
    const copied = await page.evaluate(() => (window as unknown as { copiedLink: string }).copiedLink);
    expect(copied).toBe(new URL(`/chat/${session}`, page.url()).href);
    await expect(page.getByText("Conversation link copied. Access is still required.", { exact: true })).toBeVisible();
    expect(writes).toEqual([]);
    expect(errors).toEqual([]);
  });
}

for (const width of [320, 390, 1024]) {
  for (const locale of ["en", "zh"] as const) {
    test(`task topbar keeps long titles, tabs and menus usable (${width}px ${locale})`, async ({ page }, info) => {
      await page.setViewportSize({ width, height: 844 });
      const taskTitle = locale === "zh" ? "核查多智能体研究任务的全部来源与最终交付，保留上下文并继续审阅，确认完整任务标题在窄屏也可以查看" : "Review all source references and final deliveries for the collaborative research task, preserving the complete conversation context and outstanding follow-up work";
      const { errors } = await fixture(page, { title: taskTitle, locale });
      const header = page.getByTestId("task-topbar");
      const trigger = header.getByTestId("task-title-menu");
      await expect(trigger).toHaveAttribute("title", taskTitle);
      expect((await header.boundingBox())!.height).toBeLessThanOrEqual(96);
      expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
      await trigger.click();
      const menu = page.getByRole("menu", { name: locale === "zh" ? /^任务菜单:/ : /^Task menu:/ });
      await expect(menu).toContainText(taskTitle);
      const box = (await menu.boundingBox())!;
      expect(box.x).toBeGreaterThanOrEqual(0); expect(box.x + box.width).toBeLessThanOrEqual(width);
      await page.keyboard.press("Escape");
      await expect(trigger).toBeFocused();
      const tabs = header.getByRole("tablist");
      await tabs.getByRole("tab").first().focus(); await page.keyboard.press("End");
      await expect(tabs.getByRole("tab", { name: "Canvas", exact: true })).toBeFocused();
      await page.screenshot({ path: info.outputPath(`task-topbar-${width}-${locale}.png`) });
      if (width < 768) {
        const navigation = header.getByRole("button", { name: locale === "zh" ? "打开导航" : "Open navigation", exact: true });
        await navigation.click();
        const drawer = page.getByRole("dialog");
        await expect(drawer).toBeVisible();
        await page.keyboard.press("Escape");
        await expect(drawer).not.toBeVisible(); await expect(navigation).toBeFocused();
      }
      expect(errors).toEqual([]);
    });
  }
}

test("task topbar reflects collaborator state and reports clipboard failure", async ({ page }) => {
  const state = await fixture(page);
  const status = page.getByTestId("task-header-status");
  await expect(status).toHaveText("Working");
  state.agents[0].state = "completed";
  await page.reload(); await expect(status).toHaveText("Results ready");
  state.agents[1].state = "failed";
  await page.reload(); await expect(status).toHaveText("Needs attention");
  await page.evaluate(() => Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async () => { throw new Error("clipboard unavailable"); } } }));
  await page.getByTestId("task-title-menu").click();
  await page.getByRole("menuitem", { name: "Copy conversation link", exact: true }).click();
  await expect(page.getByText("Could not copy. Copy the conversation address from your browser.", { exact: true })).toBeVisible();
  expect(state.errors).toEqual([]);
});

test("task topbar new conversation and shell navigation keep their own utilities", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { errors, writes } = await fixture(page, { empty: true });
  await page.getByTestId("task-title-menu").click();
  await expect(page.getByRole("menuitem", { name: "View latest result", exact: true })).toBeDisabled();
  await page.getByRole("menuitem", { name: "New conversation", exact: true }).click();
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByTestId("task-topbar")).toBeVisible();
  await expect(page.getByTestId("task-topbar").getByRole("tablist")).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Inbox", exact: true })).toHaveCount(1);
  await page.getByRole("button", { name: "Open navigation", exact: true }).click();
  await page.getByRole("dialog").getByRole("link", { name: "Settings", exact: true }).click();
  await expect(page).toHaveURL(/\/settings/);
  await expect(page.getByTestId("task-topbar")).toHaveCount(0);
  await expect(page.getByTestId("shell-utility-bar")).toBeVisible();
  await expect(page.getByRole("link", { name: "Inbox", exact: true })).toHaveCount(1);
  await page.getByRole("button", { name: "Open navigation", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: "Open navigation", exact: true })).toBeFocused();
  expect(writes).toEqual([]); expect(errors).toEqual([]);
});

for (const width of [1440, 1024, 390]) {
  test(`workspace vertical budget ${width}px`, async ({ page }, info) => {
    await page.setViewportSize({ width, height: 900 });
    const { errors } = await fixture(page, { rich: true });
    const header = page.getByTestId("task-topbar");
    const composer = page.locator('[data-chat-composer="docked"]');
    const taskBar = page.getByTestId("agent-task-bar");
    const baseline = { width, header: (await header.boundingBox())!.height,
      composer: (await composer.boundingBox())!.height, taskBar: (await taskBar.boundingBox())!.height };
    await page.screenshot({ path: info.outputPath(`conversation-${width}.png`) });
    await page.locator('#chat-workspace-panel-conversation [data-testid="open-result-canvas"]').first().click();
    const canvas = page.getByTestId("canvas-workspace");
    const body = page.getByTestId("canvas-result-body");
    await expect(body).toBeVisible();
    const metrics = { ...baseline, resultChrome: (await body.boundingBox())!.y - (await canvas.boundingBox())!.y };
    console.log("WORKSPACE_METRICS", JSON.stringify(metrics));
    expect(metrics.header).toBeLessThanOrEqual(44);
    expect(metrics.resultChrome).toBeLessThanOrEqual(100);
    expect(metrics.taskBar).toBeLessThanOrEqual(32);
    expect(metrics.composer).toBeLessThanOrEqual(100);
    await page.screenshot({ path: info.outputPath(`reading-${width}.png`) });
    expect(errors).toEqual([]);
  });
}

const readingReport = [
  "## 研究报告 · 证据核查",
  "**结论：报告的主要论点已有对应材料，发布前还需要补齐两项来源记录。** 以下是界面验收示例，不是实时市场分析。",
  "### 核查概览",
  "| 核查内容 | 当前记录 | 后续动作 |\n| --- | --- | --- |\n| 来源对应关系 | 已整理 | 逐条复核引用 |\n| 数据时间范围 | 有缺失项 | 补充原始时间戳 |\n| 推断与事实区分 | 已标注 | 保留不确定性 |",
  "### 研究员的发现",
  "已把论点与引用逐一对应。报告保留了原始资料的边界，没有用一个确定性结论覆盖不同来源之间的差异。来源入口直接指向报告中实际引用的链接。",
  "参见[来源记录 A](https://example.com/evidence-a)与[来源记录 B](https://example.com/evidence-b)。重复引用[记录 A](https://example.com/evidence-a)不会增加来源数量。",
  "### 审阅员的意见",
  "两处数据的观察窗口不同，不能直接比较。需要先对齐时间范围，再决定是否保留报告中的比较结论。成员之间的澄清消息与结果仍按执行轮次保存。",
  "### 需要人工确认",
  "请确认缺失的原始链接及统计时间。这些信息未核实前，应保留待确认标记，而不是自动标为完成。",
  "### 下一步",
  "补齐材料后，可以在同一成员对话中继续核查。已保存的结果仍可回看，新的结论不会替换旧轮次的记录。",
  "```text\n## 这只是代码示例，不应出现在目录\n```",
].join("\n\n");

for (const view of [{ width: 1440, theme: "dark" }, { width: 1440, theme: "light" }, { width: 390, theme: "dark" }, { width: 320, theme: "dark" }]) {
  test(`reading workspace outline and source navigation ${view.width}px ${view.theme}`, async ({ page }, info) => {
    await page.setViewportSize({ width: view.width, height: 900 });
    const { errors } = await fixture(page, { answer: readingReport, title: "核查研究报告并给出下一步", locale: "zh", theme: view.theme });
    const input = page.locator('[data-chat-composer="docked"] textarea');
    await input.fill("保留这条未发送的补充说明");
    await page.getByTestId("transcript-scroll").evaluate((el) => { el.scrollTop = 0; });
    await page.screenshot({ path: info.outputPath(`conversation-${view.width}-${view.theme}.png`) });
    await page.locator('#chat-workspace-panel-conversation [data-testid="open-result-canvas"]').click();
    const canvas = page.getByTestId("canvas-workspace"), body = page.getByTestId("canvas-result-body");
    await expect(body.getByRole("heading", { name: "研究报告 · 证据核查", exact: true })).toHaveCount(1);
    await expect(canvas.getByRole("combobox", { name: "预览内容" })).toBeVisible();
    expect((await page.getByTestId("result-toolbar").boundingBox())!.height).toBeLessThanOrEqual(40);
    expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
    if (view.width > 600) await page.getByTestId("transcript-scroll").evaluate((el) => { el.scrollTop = 0; });
    await page.screenshot({ path: info.outputPath(`reading-${view.width}-${view.theme}.png`) });
    const trigger = canvas.getByRole("button", { name: "目录与来源", exact: true });
    await trigger.click();
    const outline = page.getByRole("navigation", { name: "结果目录", exact: true });
    await expect(outline.getByRole("button")).toHaveCount(6);
    await expect(outline).not.toContainText("代码示例");
    await expect(page.getByRole("region", { name: "正文引用链接" }).getByRole("link")).toHaveCount(2);
    await page.screenshot({ path: info.outputPath(`outline-${view.width}-${view.theme}.png`) });
    await outline.getByRole("button", { name: "需要人工确认", exact: true }).click();
    await expect(body.getByRole("heading", { name: "需要人工确认", exact: true })).toBeFocused();
    await trigger.click(); await page.keyboard.press("Escape"); await expect(trigger).toBeFocused();
    await expect(canvas).toBeVisible();
    await canvas.getByRole("button", { name: "Markdown", exact: true }).click();
    await expect(canvas.getByRole("textbox", { name: "Markdown 源文" })).toHaveValue(readingReport);
    await trigger.click();
    await page.getByRole("navigation", { name: "结果目录" }).getByRole("button", { name: "研究报告 · 证据核查", exact: true }).click();
    await expect(body.getByRole("heading", { name: "研究报告 · 证据核查", exact: true })).toBeFocused();
    await canvas.getByRole("button", { name: "返回对应对话", exact: true }).click();
    await expect(input).toHaveValue("保留这条未发送的补充说明");
    await page.getByRole("tablist", { name: "任务工作区", exact: true }).getByRole("tab", { name: "Agents", exact: true }).click();
    await expect(page.getByTestId("agent-detail-content").getByTestId("agent-context")).toBeVisible();
    await page.screenshot({ path: info.outputPath(`members-${view.width}-${view.theme}.png`) });
    expect(errors).toEqual([]);
  });
}

test("reading workspace outline never retains headings from a different result", async ({ page }) => {
  const { errors } = await fixture(page, { answer: readingReport, multiple: true });
  await page.locator('#chat-workspace-panel-conversation [data-testid="open-result-canvas"]').first().click();
  const picker = page.getByRole("combobox", { name: "Preview item", exact: true });
  const original = await picker.inputValue();
  const outline = page.getByRole("button", { name: "Contents and sources", exact: true });
  await expect(outline).toBeEnabled();
  await chooseOption(picker, { index: 0 });
  await expect(page.getByTestId("canvas-result-body")).toHaveText("Second review result.");
  await expect(outline).toBeDisabled();
  await chooseOption(picker, original);
  await expect(outline).toBeEnabled(); await outline.click();
  await expect(page.getByRole("navigation", { name: "Result outline" }).getByRole("button")).toHaveCount(6);
  expect(errors).toEqual([]);
});

test("reading history is not moved by streaming or completion of an asynchronous turn", async ({ page }) => {
  const { errors } = await fixture(page, { answer: [readingReport, readingReport, readingReport].join("\n\n") });
  let release: (() => void) | undefined;
  let started = false;
  const streamed: { seq: number; kind: string; text: string; event_id: string }[] = [];
  await page.route("**/api/proxy/agent/stream/events**", async (route) => {
    const fresh = streamed.splice(0);
    await route.fulfill({ json: { events: fresh, latest_seq: fresh.at(-1)?.seq || 0 } });
  });
  await page.route("**/api/proxy/agent/run_turn_internal", async (route) => {
    started = true; await new Promise<void>((resolve) => { release = resolve; });
    await route.fulfill({ json: { turn_id: "history-reading-followup", reply_text: "A new result arrived while you were reading history.", actions: [], blocks: [] } });
  });
  const input = page.locator('[data-chat-composer="docked"] textarea'), scroll = page.getByTestId("transcript-scroll");
  await input.fill("Continue with one verification"); await input.press("Enter");
  await expect.poll(() => started).toBe(true);
  try {
    await scroll.evaluate((el) => { el.scrollTop = 0; });
    await expect(page.getByTestId("jump-to-latest")).toBeVisible();
    const position = await scroll.evaluate((el) => el.scrollTop);
    streamed.push({ seq: 1, kind: "message.delta", text: "Live verification update while reading history.", event_id: "reading-live-1" });
    await expect(page.locator('#chat-workspace-panel-conversation [data-turn-loading="true"]')).toContainText("Live verification update");
    await expect.poll(() => scroll.evaluate((el) => el.scrollTop)).toBe(position);
    release!();
    await expect(page.locator('#chat-workspace-panel-conversation [data-turn-section="reply"]').last()).toContainText("A new result arrived");
    await expect.poll(() => scroll.evaluate((el) => el.scrollTop)).toBe(position);
    await page.getByTestId("jump-to-latest").click();
    await expect.poll(() => scroll.evaluate((el) => el.scrollHeight - el.clientHeight - el.scrollTop)).toBeLessThan(3);
    expect(errors).toEqual([]);
  } finally { release?.(); }
});
